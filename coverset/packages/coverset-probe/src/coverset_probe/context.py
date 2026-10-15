"""Request context: which trace a call belongs to, whether that trace is sampled, and who it is for.

A *flow* is everything one request (one trace) did. The middleware (:mod:`coverset_probe.middleware`)
or the :func:`trace` context manager opens a :class:`TraceContext` in a :class:`~contextvars.ContextVar`
for the duration of a request; the tracer's callback reads it on every call of a target function
(one ``ContextVar.get``, about 30 ns).

* **Head sampling by trace.** The keep-or-drop decision is made once, when the trace starts, so a
  sampled trace records every call it makes (up to ``max_calls_per_trace``) and its shape vector is
  complete. With a W3C ``traceparent`` header the decision is a function of the trace id (the
  OpenTelemetry ``TraceIdRatioBased`` rule: the low 64 bits as a fraction of 2^64), so every service
  in a call chain that runs the probe at the same rate keeps the same traces. Without one the
  decision is random.
* **Witnesses for k-anonymity.** A shape is exported only after ``k`` distinct witnesses showed it.
  The witness is a salted 64-bit hash of the *subject* when the application names one (a user or
  tenant id, hashed here and never stored), else of the trace id. Raw trace ids, subjects and
  header values never leave this module: only the hashes do, and only into in-process sets.
* **Outside any trace** (a background job without :func:`trace`, a CLI) calls are sampled one by one
  and each sampled call is its own witness, so ``k`` then counts calls, not people. The design doc
  says when that is acceptable.
"""

from __future__ import annotations

import contextvars
import hashlib
import os
import random
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

__all__ = [
    "TRACE",
    "TraceContext",
    "begin_trace",
    "parse_traceparent",
    "trace",
    "trace_id_ratio_sampled",
]

_TRACEPARENT: Final = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})(?:-.*)?$")
_ZERO_TRACE: Final = "0" * 32
_ZERO_SPAN: Final = "0" * 16
_SALT: Final = os.urandom(16)
"""Per-process salt of witness hashes: a hash means nothing outside the process that made it."""
_RNG: Final = random.Random(os.urandom(16))

TRACE: contextvars.ContextVar[TraceContext | None] = contextvars.ContextVar(
    "coverset_probe_trace", default=None
)
"""The current request's context, or None outside any trace."""


@dataclass(slots=True)
class TraceContext:
    """One request (trace) as the probe sees it."""

    sampled: bool
    witness: int
    """Salted 64-bit hash of the subject (when named) or the trace id; the k-anonymity unit."""
    calls: int = 0
    """Calls recorded for this trace so far (capped by the probe's ``max_calls_per_trace``)."""
    funcs: set[int] = field(default_factory=set)
    """Indices of the target functions this trace called (its flow), filled while sampled."""


def parse_traceparent(header: str | bytes | None) -> tuple[str, bool] | None:
    """``(trace_id, sampled_flag)`` of a W3C ``traceparent`` header, or None when it is malformed.

    Version ``ff`` and all-zero ids are invalid per the W3C Trace Context spec; a header longer
    than 512 characters is refused before the regular expression sees it.
    """
    if header is None:
        return None
    if isinstance(header, bytes):
        try:
            header = header.decode("ascii")
        except UnicodeDecodeError:
            return None
    if len(header) > 512:
        return None
    match = _TRACEPARENT.match(header.strip().lower())
    if match is None:
        return None
    version, trace_id, span_id, flags = match.groups()
    if version == "ff" or trace_id == _ZERO_TRACE or span_id == _ZERO_SPAN:
        return None
    return trace_id, bool(int(flags, 16) & 1)


def trace_id_ratio_sampled(trace_id: str, rate: float) -> bool:
    """OpenTelemetry's ``TraceIdRatioBased`` decision: keep when the low 64 bits < rate x 2^64."""
    if rate >= 1.0:
        return True
    if rate <= 0.0:
        return False
    return int(trace_id[16:], 16) < int(rate * (1 << 64))


def _witness(value: str) -> int:
    digest = hashlib.blake2b(value.encode("utf-8", "surrogatepass"), digest_size=8, key=_SALT).digest()
    return int.from_bytes(digest, "big")


def begin_trace(
    rate: float, traceparent: str | bytes | None = None, subject: str | None = None
) -> TraceContext:
    """A new context: sampled by trace id when ``traceparent`` parses, else at random with ``rate``.

    ``subject`` (a user or tenant id) becomes the k-anonymity witness when given; only its salted
    hash is kept.
    """
    parsed = parse_traceparent(traceparent)
    if parsed is not None:
        trace_id = parsed[0]
        sampled = trace_id_ratio_sampled(trace_id, rate)
    else:
        trace_id = f"{_RNG.getrandbits(128):032x}"
        sampled = _RNG.random() < rate
    witness = _witness("s\0" + subject) if subject else _witness("t\0" + trace_id)
    return TraceContext(sampled=sampled, witness=witness)


@contextmanager
def trace(
    traceparent: str | bytes | None = None, *, subject: str | None = None
) -> Iterator[TraceContext | None]:
    """Run a block as one flow (a background job, a queue message): the WSGI/ASGI middleware's core.

    Yields None and does nothing when no probe is running, so the call sites can stay in place
    when the probe is switched off.
    """
    from coverset_probe.probe import current

    probe = current()
    if probe is None:
        yield None
        return
    ctx = begin_trace(probe.trace_rate, traceparent, subject)
    token = TRACE.set(ctx)
    try:
        yield ctx
    finally:
        TRACE.reset(token)
        probe.end_trace(ctx)
