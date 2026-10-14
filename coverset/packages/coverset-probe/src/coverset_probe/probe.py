"""The probe's lifecycle: start the tracer, flush a window every ``window_s`` seconds, police overhead.

One :class:`Probe` per process (``start()`` refuses a second). The flush thread is a daemon that
wakes every ``window_s`` seconds, rotates the tracer's window, builds the payload (k-anonymity gate,
optional noise) and hands it to every exporter. ``stop()`` flushes the last partial window.

**Hot functions are shed first.** At each flush a function whose calls alone cost more than a
quarter of the budget is dropped for the rest of the process (``probe.shed`` lists it), so one tight
loop does not switch the whole probe off.

**The overhead self-check.** At each flush the probe estimates its own cost over the window:
``callbacks x fast_call_cost + slow_seconds``, where ``fast_call_cost`` is calibrated at start (the
measured extra time of an unsampled call under ``sys.monitoring`` on a throwaway function) and
``slow_seconds`` is timed directly around every recorded call. Divided by the window's wall time
this is the fraction of one CPU core the probe used. Above ``max_overhead`` (default 2 %) the probe
switches itself off, says why in that window's payload (``probe.disabled``) and exports nothing
further. The estimate leaves out cache effects on the application's own code, which is why the
default bound is low; ``bench/bench_overhead.py`` measures the real cost end to end.

**Exit.** ``start()`` registers an ``atexit`` hook (once per process) that stops the running probe
and exports its last partial window, so a worker recycled by ``max_requests``, a deploy, a pod
restart or a batch job shorter than ``window_s`` still delivers what it saw. The hook waits at most
:data:`EXIT_JOIN_S` for the flush thread. It does not run on ``os._exit`` or a fatal signal.

Errors are counted, never raised into the application: a tracer error budget (``max_errors``)
switches the tracer off, and an exporter error is counted in ``probe.export_errors`` of the next
payload.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import random
import re
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from time import perf_counter
from typing import Any

from coverset_probe.context import TraceContext
from coverset_probe.exporters import Exporter
from coverset_probe.tracer import DEFAULT_EXCLUDE_PARAMS, ProbeTracer
from coverset_probe.window import FuncStats, KGate, build_payload

__all__ = ["Probe", "current", "start", "stop"]

SHED_SHARE = 0.25
"""A function is shed when its unsampled calls alone cost more than this share of ``max_overhead``."""
_LOCK = threading.Lock()
_CURRENT: Probe | None = None
_FORK_HOOK: list[bool] = []
_EXIT_HOOK: list[bool] = []
EXIT_JOIN_S = 2.0
"""How long the exit hook waits for a flush already in progress on the flush thread."""


def _after_fork() -> None:
    global _LOCK
    _LOCK = threading.Lock()
    if _CURRENT is not None:
        _CURRENT.after_fork_in_child()


class Probe:
    """The tracer, the k-anonymity gate, the exporters and the flush thread of one process."""

    def __init__(
        self,
        *,
        modules: Sequence[str],
        export: Exporter | Sequence[Exporter] | None = None,
        service: str | None = None,
        env: str = "production",
        release: str | None = None,
        sample_rate: float = 0.01,
        trace_sample_rate: float | None = None,
        k_anonymity: int = 5,
        window_s: float = 60.0,
        source_root: str | os.PathLike[str] | None = None,
        exclude_modules: Sequence[str] = (),
        exclude_params: str | re.Pattern[str] | None = DEFAULT_EXCLUDE_PARAMS,
        record_returns: bool = True,
        settle: int = 64,
        max_samples_per_func: int = 1000,
        max_obs_per_window: int = 20_000,
        max_calls_per_trace: int = 256,
        max_keys: int = 20_000,
        max_overhead: float = 0.02,
        min_overhead_wall_s: float = 1.0,
        dp_epsilon: float | None = None,
        seed: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.exporters: list[Exporter] = (
            [] if export is None else list(export) if isinstance(export, Sequence) else [export]
        )
        self.service = service or os.environ.get("OTEL_SERVICE_NAME") or "service"
        self.env = env
        self.release = release
        self.window_s = window_s
        self.max_overhead = max_overhead
        self.min_overhead_wall_s = min_overhead_wall_s
        self.dp_epsilon = dp_epsilon
        self._rng = random.Random(seed) if seed is not None else random.Random(os.urandom(16))
        self._clock = clock
        self.tracer = ProbeTracer(
            modules=modules,
            exclude_modules=exclude_modules,
            source_root=source_root,
            sample_rate=sample_rate,
            trace_rate=trace_sample_rate,
            settle=settle,
            max_samples_per_func=max_samples_per_func,
            max_obs_per_window=max_obs_per_window,
            max_calls_per_trace=max_calls_per_trace,
            max_keys=max_keys,
            exclude_params=exclude_params,
            record_returns=record_returns,
            rng=random.Random(self._rng.getrandbits(64)),
            clock=clock,
        )
        self.gate = KGate(k_anonymity)
        self.settings: dict[str, Any] = {
            "sample_rate": sample_rate,
            "trace_sample_rate": self.tracer.trace_rate,
            "k": self.gate.k,
            "window_s": window_s,
        }
        self.export_errors = 0
        self.windows = 0
        self.last_overhead: float | None = None
        self._slow_mark = 0.0
        self._wall_mark = perf_counter()
        self._stop = threading.Event()
        self._flush_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._finished = False

    @property
    def trace_rate(self) -> float:
        """The head-sampling probability of a new trace."""
        return self.tracer.trace_rate

    @property
    def active(self) -> bool:
        """Whether the tracer is recording (False once stopped or self-disabled)."""
        return self.tracer.active

    @property
    def disabled_reason(self) -> str | None:
        """Why the tracer is off, when it switched itself off (or could not start)."""
        return self.tracer.disabled_reason

    def start(self, *, thread: bool = True) -> bool:
        """Start tracing (and the flush thread unless ``thread`` is False); False if tracing cannot start."""
        if not self.tracer.start():
            return False
        self._wall_mark = perf_counter()
        if thread and self.window_s > 0:
            self._thread = threading.Thread(target=self._run, name="coverset-probe-flush", daemon=True)
            self._thread.start()
        return True

    def _run(self) -> None:
        while not self._stop.wait(self.window_s):
            self.flush()
            if not self.tracer.active:
                return

    def end_trace(self, ctx: TraceContext) -> None:
        """Called by the middleware when a request ends: records its flow."""
        self.tracer.end_trace(ctx)

    def flush(self) -> dict[str, Any] | None:
        """Close the current window, export it, run the overhead check; returns the payload.

        Returns None once the probe has exported its final window.
        """
        with self._flush_lock:
            if self._finished:
                return None
            window, stats, callbacks = self.tracer.rotate()
            now_wall = perf_counter()
            wall = now_wall - self._wall_mark
            slow = self.tracer.slow_seconds - self._slow_mark
            self._wall_mark, self._slow_mark = now_wall, self.tracer.slow_seconds
            overhead = (callbacks * self.tracer.fast_call_cost + slow) / wall if wall > 0 else 0.0
            self.last_overhead = overhead
            if self.tracer.active and wall >= self.min_overhead_wall_s:
                self._shed_hot(stats, wall)
            if self.tracer.active and wall >= self.min_overhead_wall_s and overhead > self.max_overhead:
                self.tracer.stop(f"overhead {overhead:.2%} of one core > {self.max_overhead:.2%}")
            health = {
                "overhead": round(overhead, 6),
                "fast_call_ns": round(self.tracer.fast_call_cost * 1e9, 1),
                "errors": self.tracer.errors,
                "export_errors": self.export_errors,
                "disabled": self.tracer.disabled_reason,
                "shed": [
                    f"{self.tracer.funcs[i].module}.{self.tracer.funcs[i].qualname}"
                    for i in self.tracer.shed_funcs
                ],
            }
            payload = build_payload(
                window,
                end=self._clock(),
                funcs=self.tracer.funcs,
                stats=stats,
                gate=self.gate,
                service=self.service,
                env=self.env,
                release=self.release,
                settings=self.settings,
                health=health,
                dp_epsilon=self.dp_epsilon,
                call_sensitivity=float(self.tracer.max_calls_per_trace),
                rng=self._rng,
            )
            self.windows += 1
            for exporter in self.exporters:
                try:
                    exporter.export(payload)
                except Exception:
                    self.export_errors += 1
            if not self.tracer.active:
                self._finished = True
            return payload

    def _shed_hot(self, stats: Mapping[int, FuncStats], wall: float) -> None:
        """Shed every function whose unsampled calls alone used over a quarter of the overhead budget."""
        limit = self.max_overhead * SHED_SHARE * wall
        cost = self.tracer.fast_call_cost
        for index, func_stats in stats.items():
            if func_stats.calls * cost > limit:
                self.tracer.shed(index)

    def stop(self, *, flush: bool = True, join_timeout_s: float | None = None) -> dict[str, Any] | None:
        """Stop tracing and the flush thread; export the last window when ``flush``. Idempotent.

        ``join_timeout_s`` bounds the wait for a flush in progress (default: ``max(5, window_s)``).
        """
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            timeout = max(5.0, self.window_s) if join_timeout_s is None else join_timeout_s
            self._thread.join(timeout=timeout)
        self.tracer.stop()
        payload = self.flush() if flush else None
        self._finished = True
        self.tracer.close()
        return payload

    def after_fork_in_child(self) -> None:
        """Make a forked worker's copy of the probe its own: fresh locks, window and flush thread.

        ``sys.monitoring`` state survives ``fork`` (the callbacks keep firing in the child), but the
        flush thread does not, and the parent's partial window would be exported twice. A server that
        imports the application before forking its workers (gunicorn ``--preload``) therefore gets
        one working probe per worker without calling ``start()`` again.
        """
        self._stop = threading.Event()
        self._flush_lock = threading.Lock()
        self.tracer._discover_lock = threading.Lock()  # another thread may have held it at fork time
        self.tracer.rotate()  # drop the parent's partial window
        self._wall_mark, self._slow_mark = perf_counter(), self.tracer.slow_seconds
        self._thread = None
        if self.tracer.active and self.window_s > 0 and not self._finished:
            self._thread = threading.Thread(target=self._run, name="coverset-probe-flush", daemon=True)
            self._thread.start()

    def stats(self) -> Mapping[str, Any]:
        """A snapshot for health endpoints and tests (no shape data)."""
        return {
            "active": self.active,
            "disabled": self.disabled_reason,
            "functions": len(self.tracer.funcs),
            "windows": self.windows,
            "errors": self.tracer.errors,
            "export_errors": self.export_errors,
            "overhead": self.last_overhead,
            "fast_call_ns": round(self.tracer.fast_call_cost * 1e9, 1),
        }


def start(modules: Sequence[str], **options: Any) -> Probe:
    """Start the process's probe (see :class:`Probe` for the options); raises if one is running.

    ``coverset_probe.start(modules=["myapp"], sample_rate=0.01, k_anonymity=5,
    export=JsonlExporter("/var/log/myapp/flows.jsonl"), window_s=60)``. If tracing cannot start
    (Python < 3.12, no free ``sys.monitoring`` tool id) the returned probe is inactive and
    ``disabled_reason`` says why; the application is never affected.
    """
    global _CURRENT
    with _LOCK:
        if _CURRENT is not None:
            raise RuntimeError("a coverset probe is already running in this process")
        probe = Probe(modules=modules, **options)
        if probe.start():
            _CURRENT = probe
            if not _FORK_HOOK and hasattr(os, "register_at_fork"):
                os.register_at_fork(after_in_child=_after_fork)
                _FORK_HOOK.append(True)
            if not _EXIT_HOOK:
                atexit.register(_at_exit)
                _EXIT_HOOK.append(True)
        return probe


def stop(*, flush: bool = True, join_timeout_s: float | None = None) -> dict[str, Any] | None:
    """Stop the running probe (exporting the last window when ``flush``); None when none runs."""
    global _CURRENT
    with _LOCK:
        probe, _CURRENT = _CURRENT, None
    return probe.stop(flush=flush, join_timeout_s=join_timeout_s) if probe is not None else None


def _at_exit() -> None:
    """Interpreter exit: export the running probe's last partial window (never raises)."""
    with contextlib.suppress(Exception):  # nothing may surface at interpreter exit
        stop(join_timeout_s=EXIT_JOIN_S)


def current() -> Probe | None:
    """The running probe, or None."""
    return _CURRENT
