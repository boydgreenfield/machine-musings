"""The production tracer core: pytest-coverset's shape tracer, adapted for a process that never ends.

What is kept from ``pytest_coverset.shapes`` (read that module's docstring first):

* A ``sys.monitoring`` tool (id 4, else 5, else 2) with a global ``PY_START`` used only to discover
  code objects; a non-target gets ``DISABLE`` once and is forgotten, a target gets a local
  ``PY_START`` event.
* Shape keys are computed only for exact builtin types; no user dunder is ever called; ``self`` and
  ``cls`` are recorded as their class (``<receiver>``); ``*args``/``**kwargs`` as ``*name``/``**name``.
* Every callback is wrapped in ``try/except`` (errors are counted, not raised; ``KeyboardInterrupt``
  propagates) and keeps no reference to argument values once it returns.

What changes for production:

* **Targets are an allow-list of modules** (``modules``), never "everything in the repo": a
  service's virtualenv is not a repository checkout. The probe's own package is never a target.
* **Sampling is per call or per trace, not per test.** Every call of a target runs the *fast path*:
  one dict lookup, one counter increment, one ``ContextVar.get`` and a compare. Outside a trace a
  call is recorded when the function's call counter reaches its next sample point (geometric gaps,
  i.e. Bernoulli sampling at the function's current rate); inside a trace it is recorded when the
  trace was head-sampled (:mod:`coverset_probe.context`), up to ``max_calls_per_trace`` calls.
* **Adaptive rate instead of a permanent cap.** After ``settle`` recorded calls of a function with
  no new ``(parameter, shape)``, its per-call rate is divided by four (down to
  ``sample_rate / 64``); a new shape restores the base rate. Each recorded call carries the weight
  ``1 / rate``, so counts stay unbiased (:mod:`coverset_probe.window`).
* **Hot functions are shed.** Every call of a target costs 80 to 100 ns (Python 3.13, Apple M4 Max,
  ``bench/bench_overhead.py``) whether or not it is sampled. :meth:`ProbeTracer.shed` drops a function whose calls alone cost
  more than its share of the overhead budget (the probe decides at each flush): its events go off,
  its next call returns ``DISABLE``, and the payload lists it under ``probe.shed`` for the rest of
  the process lifetime. A tight loop in an allow-listed module therefore costs one window, not
  the service.
* **Returns are recorded for sampled calls only.** ``PY_RETURN`` is switched on for a code object
  while one of its sampled calls is in flight (the frame is remembered) and off again when the last
  one returns, so an unsampled call pays for one callback, not two (switching the event on and
  off re-instruments the code object, about 1 us per recorded call; ``record_returns=False``
  saves it). A sampled call that raises leaves its entry behind
  until the window rotates, which also switches the event off.
* **Caps reset every window.** ``max_samples_per_func`` recorded calls per function per window and
  ``max_obs_per_window`` per process (the hard budget) switch recording off until the next window;
  the fast path still counts calls, so a function's exact call count survives the cap. Capping
  never uses ``DISABLE`` (re-enabling a disabled location needs ``sys.monitoring.restart_events``,
  which would touch every other tool in the process).
* **Values are never kept, nor printed.** There is no example repr in production mode: only shape
  keys leave the callback. Parameters whose name matches ``exclude_params`` (passwords, tokens,
  keys, cookies, sessions, credentials by default) are not observed at all.
* **Function identity is (module, qualname)**, which survives packaging (a wheel in site-packages
  has no repository path); a path relative to ``source_root`` is added when the file lives under it.
  Callers are exported as a 16-hex-digit blake2b of ``module\\0qualname`` (the importer recomputes
  the same hash over the run's functions to resolve them).
"""

from __future__ import annotations

import hashlib
import inspect
import math
import os
import random
import re
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from time import perf_counter
from types import CodeType, FrameType
from typing import Any, Final

from coverset_probe.context import TRACE, TraceContext
from coverset_probe.shapes import object_shape_key, shape_key
from coverset_probe.window import EXTERNAL_CALLER, FuncInfo, FuncStats, Window

__all__ = [
    "DEFAULT_EXCLUDE_PARAMS",
    "RECEIVER",
    "RETURN",
    "TOOL_ID_PREFERENCE",
    "ProbeTracer",
    "caller_hash",
]

TOOL_ID_PREFERENCE: Final = (4, 5, 2)
TOOL_NAME: Final = "coverset-probe"
RECEIVER: Final = "<receiver>"
RETURN: Final = "return"
DEFAULT_EXCLUDE_PARAMS: Final = r"(?i)pass|secret|token|key|auth|cookie|session|cred"
"""Parameter names never observed: the plugin's ``SECRET_NAME_RE`` (its repr-redaction rule)."""
MAX_SEEN_PER_FUNC: Final = 4096
"""Distinct ``(parameter, shape)`` pairs remembered per function for the adaptive rate."""
MIN_RATE_DIVISOR: Final = 64
MAX_PENDING_RETURNS: Final = 64
"""Sampled calls of one function awaiting their return (deep recursion records the outer ones)."""
_CO_OPTIMIZED: Final = inspect.CO_OPTIMIZED
_CO_VARARGS: Final = inspect.CO_VARARGS
_CO_VARKEYWORDS: Final = inspect.CO_VARKEYWORDS
_CO_NO_RETURN_SHAPE: Final = inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR
_RECEIVER_NAMES: Final = frozenset({"self", "cls"})
_LAMBDA: Final = "<lambda>"
_LOCALS: Final = "<locals>"
_MAX_CALLER_HOPS: Final = 4
_OWN_PACKAGE: Final = __name__.partition(".")[0]
_TYPE_MRO = type.__dict__["__mro__"]
_TYPE_NAME = type.__dict__["__name__"]


def caller_hash(module: str, qualname: str) -> str:
    """The exported id of a function as a caller: blake2b-8 of ``module\\0qualname``, 16 hex digits."""
    return hashlib.blake2b(f"{module}\0{qualname}".encode(), digest_size=8).hexdigest()


@dataclass(slots=True)
class _Target:
    """Sampling state of one target code object (held for the process lifetime)."""

    index: int
    code: CodeType
    params: tuple[tuple[str, str], ...]
    owner: str | None
    record_return: bool
    caller_key: str
    rate: float
    calls: int = 0
    """Calls this window (every call, sampled or not)."""
    next_at: int = 1
    rcalls: int = 0
    """Return callbacks that fired this window (only while a sampled call was in flight)."""
    pending: dict[int, tuple[float, int]] = field(default_factory=dict)
    """Frame id -> (weight, witness) of this function's sampled calls still in flight."""
    sampled: int = 0
    capped: bool = False
    since_new: int = 0
    seen: set[tuple[str, str]] = field(default_factory=set)


class ProbeTracer:
    """Samples argument and return shapes of allow-listed modules' functions into a :class:`Window`."""

    def __init__(
        self,
        *,
        modules: Sequence[str],
        exclude_modules: Sequence[str] = (),
        source_root: str | os.PathLike[str] | None = None,
        sample_rate: float = 0.01,
        trace_rate: float | None = None,
        settle: int = 64,
        max_samples_per_func: int = 1000,
        max_obs_per_window: int = 20_000,
        max_calls_per_trace: int = 256,
        max_keys: int = 20_000,
        exclude_params: str | re.Pattern[str] | None = DEFAULT_EXCLUDE_PARAMS,
        record_returns: bool = True,
        max_errors: int = 1000,
        rng: random.Random | None = None,
        clock: Callable[[], float],
        tool_ids: Sequence[int] = TOOL_ID_PREFERENCE,
    ) -> None:
        if not modules:
            raise ValueError("pass the modules to observe (an allow-list such as ['myapp'])")
        if not 0.0 < sample_rate <= 1.0:
            raise ValueError("sample_rate must be in (0, 1]")
        self.modules = tuple(modules)
        self.exclude_modules = tuple(exclude_modules)
        self.source_root = os.path.realpath(source_root) if source_root is not None else None
        self.sample_rate = sample_rate
        self.trace_rate = sample_rate if trace_rate is None else trace_rate
        self.min_rate = sample_rate / MIN_RATE_DIVISOR
        self.settle = settle
        self.max_samples_per_func = max_samples_per_func
        self.max_obs_per_window = max_obs_per_window
        self.max_calls_per_trace = max_calls_per_trace
        self.max_errors = max_errors
        self.exclude_params = (
            re.compile(exclude_params) if isinstance(exclude_params, str) else exclude_params
        )
        self.record_returns = record_returns
        self._rng = rng or random.Random(os.urandom(16))
        self._clock = clock
        self._tool_ids = tuple(tool_ids)
        self.tool_id: int | None = None
        self.disabled_reason: str | None = None
        self.errors = 0
        self.slow_seconds = 0.0
        self.fast_call_cost = 0.0
        self.funcs: list[FuncInfo] = []
        self._targets: dict[int, _Target] = {}
        self._states: list[_Target] = []
        self._shed: set[int] = set()
        self._discover_lock = threading.Lock()
        self.shed_funcs: list[int] = []
        """Indices (into :attr:`funcs`) of functions shed for being too hot to observe."""
        self.window = Window(clock(), max_keys=max_keys)
        self._max_keys = max_keys
        self._active = False
        self._registered = False

    # -- lifecycle --------------------------------------------------------------------------------

    def start(self) -> bool:
        """Claim a tool id, calibrate the fast path, enable discovery; False with a reason if not possible."""
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is None:
            self.disabled_reason = "sys.monitoring is unavailable (Python < 3.12)"
            return False
        owners: list[str] = []
        for tid in self._tool_ids:
            try:
                monitoring.use_tool_id(tid, TOOL_NAME)
            except ValueError:
                owners.append(f"{tid}={monitoring.get_tool(tid)!r}")
                continue
            self.tool_id = tid
            break
        if self.tool_id is None:
            self.disabled_reason = "no free sys.monitoring tool id (in use: " + ", ".join(owners) + ")"
            return False
        events = monitoring.events
        on_start, on_return = self._callbacks()
        monitoring.register_callback(self.tool_id, events.PY_START, on_start)
        monitoring.register_callback(self.tool_id, events.PY_RETURN, on_return)
        self._registered = True
        self._active = True
        self.fast_call_cost = self._calibrate()
        monitoring.set_events(self.tool_id, events.PY_START)
        return True

    def stop(self, reason: str | None = None) -> None:
        """Switch every event off (global and local); aggregates are kept. Idempotent."""
        if reason is not None and self.disabled_reason is None:
            self.disabled_reason = reason
        if not self._active or self.tool_id is None:
            return
        self._active = False
        monitoring = sys.monitoring
        monitoring.set_events(self.tool_id, 0)
        for state in self._states:
            monitoring.set_local_events(self.tool_id, state.code, 0)

    def close(self) -> None:
        """Stop, unregister the callbacks and free the tool id. Idempotent."""
        self.stop()
        if self.tool_id is None:
            return
        monitoring = sys.monitoring
        if self._registered:
            monitoring.register_callback(self.tool_id, monitoring.events.PY_START, None)
            monitoring.register_callback(self.tool_id, monitoring.events.PY_RETURN, None)
            self._registered = False
        if monitoring.get_tool(self.tool_id) == TOOL_NAME:
            monitoring.free_tool_id(self.tool_id)
        self.tool_id = None

    @property
    def active(self) -> bool:
        """Whether events are switched on."""
        return self._active

    def rotate(self) -> tuple[Window, dict[int, FuncStats], int]:
        """Swap in a fresh window; return the finished one, per-function counters and total callbacks.

        Callers in other threads may still be adding to the old window for a moment; such a late
        addition is lost, never double-counted.
        """
        old = self.window
        self.window = Window(self._clock(), max_keys=self._max_keys)
        stats: dict[int, FuncStats] = {}
        callbacks = 0
        for state in self._states:
            if id(state.code) in self._shed:
                continue
            calls, rcalls, sampled, capped = state.calls, state.rcalls, state.sampled, state.capped
            state.calls = state.rcalls = state.sampled = 0
            state.capped = False
            state.next_at = self._gap(state.rate)
            if state.pending:  # sampled calls that raised: their returns will never come
                state.pending.clear()
                if self._active:
                    self._returns_off(state)
            callbacks += calls + rcalls
            if calls:
                stats[state.index] = FuncStats(calls, sampled, capped)
        return old, stats, callbacks

    def shed(self, index: int) -> None:
        """Stop observing function ``index`` for the rest of the process (it is too hot)."""
        state = self._states[index]
        if id(state.code) in self._shed:
            return
        self._shed.add(id(state.code))
        self._targets.pop(id(state.code), None)
        state.pending.clear()
        self.shed_funcs.append(index)
        if self._active:
            sys.monitoring.set_local_events(self.tool_id, state.code, 0)  # type: ignore[arg-type]

    def end_trace(self, ctx: TraceContext) -> None:
        """Record a finished trace's flow (the set of target functions it called), if it was sampled."""
        if ctx.sampled and ctx.funcs and self._active:
            self.window.add_flow(ctx.funcs, ctx.witness)

    # -- callbacks --------------------------------------------------------------------------------

    def _callbacks(
        self,
    ) -> tuple[Callable[[CodeType, int], object], Callable[[CodeType, int, object], object]]:
        """The two callbacks as closures over local names (cheaper than bound-method lookups)."""
        targets_get = self._targets.get
        trace_get = TRACE.get
        getframe = sys._getframe
        disable = sys.monitoring.DISABLE
        discover = self._discover
        sample = self._sample_call
        sample_return = self._sample_return

        def on_start(code: CodeType, offset: int) -> object:
            state = targets_get(id(code))
            if state is None:
                return discover(code, getframe(1))
            state.calls += 1
            ctx = trace_get()
            if ctx is None:
                if state.calls < state.next_at or state.capped:
                    return None
            elif not ctx.sampled or state.capped:
                return None
            sample(state, getframe(1), ctx)
            return None

        def on_return(code: CodeType, offset: int, retval: object) -> object:
            state = targets_get(id(code))
            if state is None:
                return disable
            state.rcalls += 1
            sample_return(state, getframe(1), retval)
            return None

        return on_start, on_return

    def _discover(self, code: CodeType, frame: FrameType) -> object:
        """First call of a code object: make it a target (and treat this call normally) or DISABLE it."""
        try:
            if id(code) in self._shed:
                return sys.monitoring.DISABLE
            if frame.f_code is not code or not self._active:
                return None
            module = frame.f_globals.get("__name__")
            if (
                type(module) is not str
                or not code.co_flags & _CO_OPTIMIZED
                or (code.co_name.startswith("<") and code.co_name != _LAMBDA)
                or not self._allowed(module)
            ):
                return sys.monitoring.DISABLE
            with self._discover_lock:  # two threads may meet a new function's first calls together
                state = self._targets.get(id(code))
                if state is None:
                    state = self._new_target(code, module)
                    sys.monitoring.set_local_events(self.tool_id, code, sys.monitoring.events.PY_START)  # type: ignore[arg-type]
            state.calls += 1
            ctx = TRACE.get()
            if (ctx.sampled if ctx is not None else state.calls >= state.next_at) and not state.capped:
                self._sample_call(state, frame, ctx)
            return None
        except KeyboardInterrupt:
            raise
        except BaseException:
            self._error()
            return None

    def _allowed(self, module: str) -> bool:
        if module == _OWN_PACKAGE or module.startswith(_OWN_PACKAGE + "."):
            return False
        if any(module == m or module.startswith(m + ".") for m in self.exclude_modules):
            return False
        return any(module == m or module.startswith(m + ".") for m in self.modules)

    def _new_target(self, code: CodeType, module: str) -> _Target:
        names = code.co_varnames
        n_named = code.co_argcount + code.co_kwonlyargcount
        params: list[tuple[str, str]] = [(name, name) for name in names[:n_named]]
        position = n_named
        if code.co_flags & _CO_VARARGS:
            params.append((names[position], "*" + names[position]))
            position += 1
        if code.co_flags & _CO_VARKEYWORDS:
            params.append((names[position], "**" + names[position]))
        owner = _owner_class(code) if code.co_argcount and params[0][0] in _RECEIVER_NAMES else None
        if self.exclude_params is not None:
            params = [
                (name, label)
                for i, (name, label) in enumerate(params)
                if (i == 0 and owner is not None) or not self.exclude_params.search(name)
            ]
        state = _Target(
            index=len(self._states),
            code=code,
            params=tuple(params),
            owner=owner,
            record_return=self.record_returns and not code.co_flags & _CO_NO_RETURN_SHAPE,
            caller_key=caller_hash(module, code.co_qualname),
            rate=self.sample_rate,
        )
        state.next_at = self._gap(state.rate)
        self.funcs.append(
            FuncInfo(module, code.co_qualname, code.co_firstlineno, self._rel_path(code.co_filename))
        )
        self._states.append(state)
        # The state keeps the code object alive, so its id stays unique for the process lifetime.
        self._targets[id(code)] = state
        return state

    def _rel_path(self, filename: str) -> str | None:
        if self.source_root is None or filename.startswith("<"):
            return None
        try:
            rel = os.path.relpath(os.path.realpath(filename), self.source_root)
        except ValueError:
            return None
        return None if rel.startswith("..") else rel.replace(os.sep, "/")

    def _gap(self, rate: float) -> int:
        """Calls until the next sample under Bernoulli(rate): 1 + Geometric(rate) failures."""
        if rate >= 1.0:
            return 1
        u = self._rng.random() or 1e-300
        return 1 + int(math.log(u) / math.log1p(-rate))

    def _budget_ok(self, state: _Target) -> bool:
        window = self.window
        if window.sampled_calls + window.sampled_returns >= self.max_obs_per_window:
            if not window.budget_hit:
                window.budget_hit = True
                for other in self._states:
                    other.capped = True
            return False
        if state.sampled >= self.max_samples_per_func:
            state.capped = True
            return False
        return True

    def _sample_call(self, state: _Target, frame: FrameType, ctx: TraceContext | None) -> None:
        started = perf_counter()
        values: Any = None
        try:
            if not self._budget_ok(state):
                return
            if ctx is None:
                weight = 1.0 / state.rate
                witness = self._rng.getrandbits(64)
                state.next_at = state.calls + self._gap(state.rate)
            else:
                if ctx.calls >= self.max_calls_per_trace:
                    return
                ctx.calls += 1
                ctx.funcs.add(state.index)
                weight = 1.0 / self.trace_rate
                witness = ctx.witness
            window = self.window
            state.sampled += 1
            window.sampled_calls += 1
            if state.record_return and len(state.pending) < MAX_PENDING_RETURNS:
                if not state.pending:
                    self._returns_on(state)
                state.pending[id(frame)] = (weight, witness)
            caller = self._caller(frame.f_back)
            values = frame.f_locals
            vector: list[tuple[str, str]] = []
            new = False
            for position, (name, label) in enumerate(state.params):
                try:
                    value = values[name]
                except KeyError:
                    continue
                try:
                    receiver = _receiver_class(value, state.owner) if position == 0 and state.owner else None
                    if receiver is not None:
                        pair = (RECEIVER, object_shape_key(receiver))
                    else:
                        pair = (label, shape_key(value))
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self._error()
                    continue
                finally:
                    value = None
                window.add_shape(state.index, pair[0], pair[1], weight, witness, caller)
                vector.append(pair)
                if pair not in state.seen and len(state.seen) < MAX_SEEN_PER_FUNC:
                    state.seen.add(pair)
                    new = True
            window.add_vector(state.index, tuple(vector), weight, witness)
            if ctx is None:
                self._adapt(state, new)
        except KeyboardInterrupt:
            raise
        except BaseException:
            self._error()
        finally:
            if type(values) is dict:
                # Python 3.12: f_locals is a dict snapshot cached on the frame (see the plugin's
                # tracer); left filled it would keep every argument alive until the call returns.
                values.clear()
            values = None
            self.slow_seconds += perf_counter() - started

    def _adapt(self, state: _Target, new: bool) -> None:
        if new:
            state.since_new = 0
            if state.rate != self.sample_rate:
                state.rate = self.sample_rate
                state.next_at = state.calls + self._gap(state.rate)
            return
        state.since_new += 1
        if state.since_new >= self.settle and state.rate > self.min_rate:
            state.since_new = 0
            state.rate = max(self.min_rate, state.rate / 4)

    def _returns_on(self, state: _Target) -> None:
        events = sys.monitoring.events
        sys.monitoring.set_local_events(self.tool_id, state.code, events.PY_START | events.PY_RETURN)  # type: ignore[arg-type]

    def _returns_off(self, state: _Target) -> None:
        sys.monitoring.set_local_events(self.tool_id, state.code, sys.monitoring.events.PY_START)  # type: ignore[arg-type]

    def _sample_return(self, state: _Target, frame: FrameType, retval: object) -> None:
        """A return of ``state``'s code while a sampled call is in flight: record it if it is that call's."""
        started = perf_counter()
        try:
            entry = state.pending.pop(id(frame), None)
            if entry is not None and self._active:
                weight, witness = entry
                window = self.window
                window.sampled_returns += 1
                window.add_shape(state.index, RETURN, shape_key(retval), weight, witness, None)
            if not state.pending and self._active:
                self._returns_off(state)
        except KeyboardInterrupt:
            raise
        except BaseException:
            self._error()
        finally:
            retval = None
            self.slow_seconds += perf_counter() - started

    def _caller(self, frame: FrameType | None) -> str:
        """The calling target's hashed id; comprehension frames are skipped; anything else is external."""
        for _ in range(_MAX_CALLER_HOPS):
            if frame is None:
                break
            code = frame.f_code
            state = self._targets.get(id(code))
            if state is not None:
                return state.caller_key
            name = code.co_name
            if code.co_flags & _CO_OPTIMIZED and name.startswith("<") and name != _LAMBDA:
                frame = frame.f_back
                continue
            break
        return EXTERNAL_CALLER

    def _error(self) -> None:
        self.errors += 1
        if self.errors >= self.max_errors and self._active:
            self.stop(f"{self.errors} tracer errors")

    # -- calibration --------------------------------------------------------------------------------

    def _calibrate(self, n: int = 20_000) -> float:
        """Seconds the fast path adds to one call (and its return), measured on a throwaway function.

        Runs before global events are switched on, on a freshly compiled function that is a target
        with sampling pushed out of reach, so the measurement is exactly the unsampled path:
        ``sys.monitoring`` dispatch plus the callbacks' early return.
        """
        namespace: dict[str, Any] = {}
        exec(compile("def calibration_target(x):\n    return x\n", "<coverset-probe>", "exec"), namespace)
        func = namespace["calibration_target"]
        code: CodeType = func.__code__
        best_off = best_on = math.inf
        state = _Target(
            index=-1, code=code, params=(), owner=None, record_return=False,
            caller_key="", rate=self.sample_rate, next_at=1 << 62,
        )  # fmt: skip
        wanted = sys.monitoring.events.PY_START
        try:
            for _ in range(3):
                t0 = perf_counter()
                for i in range(n):
                    func(i)
                best_off = min(best_off, perf_counter() - t0)
                self._targets[id(code)] = state
                sys.monitoring.set_local_events(self.tool_id, code, wanted)  # type: ignore[arg-type]
                t0 = perf_counter()
                for i in range(n):
                    func(i)
                best_on = min(best_on, perf_counter() - t0)
                sys.monitoring.set_local_events(self.tool_id, code, 0)  # type: ignore[arg-type]
                del self._targets[id(code)]
        finally:
            self._targets.pop(id(code), None)
        return max(0.0, (best_on - best_off) / n)


def _owner_class(code: CodeType) -> str | None:
    """The defining class's name when ``code`` is defined directly in a class body (the plugin's rule)."""
    parent, dot, name = code.co_qualname.rpartition(".")
    if not dot or name == _LAMBDA:
        return None
    owner = parent.rpartition(".")[2]
    return None if owner == _LOCALS or owner.startswith("<") else owner


def _receiver_class(value: object, owner: str) -> type | None:
    """The class to record for ``self``/``cls``, through ``type``'s own descriptors only."""
    cls = type(value)
    receiver = value if issubclass(cls, type) else cls
    for klass in _TYPE_MRO.__get__(receiver):
        if _TYPE_NAME.__get__(klass) == owner:
            return receiver  # type: ignore[return-value]
    return None
