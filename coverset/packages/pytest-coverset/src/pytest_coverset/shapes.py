"""The sys.monitoring shape tracer: which kinds of value flowed into (and out of) each in-repo function.

Design (plan "pytest-coverset", Addendum B):

* A second ``sys.monitoring`` tool (id 4, else 5, else 2) coexists with coverage's ctrace core.
* A global ``PY_START`` is used only for discovery. The first event for a code object decides once
  whether it is a target; non-targets return ``DISABLE``. Targets get local ``PY_START | PY_RETURN``
  events and are sampled through them.
* Sampling is adaptive per code object: once ``settle`` consecutive calls produce no new shape, or
  ``max_samples`` calls were sampled, local events are switched off and the function is marked
  truncated (``cap: "sample"``). Code objects in full-trace files skip the adaptive cap and stop only
  at the much higher safety cap ``full_cap`` (``cap: "full"``). ``restart_events()`` is never called.
* Only target code objects are remembered. A non-target returns ``DISABLE`` once and is forgotten, so
  code compiled at run time (``exec``, templating) is not kept alive by the tracer.
* Every callback is guarded against re-entrancy, wrapped in ``try/except BaseException`` (counted in
  :attr:`ShapeTracer.tracer_errors`), computes features only for exact builtin types, and keeps no
  reference to argument values once it returns. Each parameter is observed in its own ``try`` so one
  unrepresentable value does not drop the call's other parameters. ``KeyboardInterrupt`` (Ctrl-C, or
  SIGTERM through the plugin's handler) is the one exception that propagates, so a stop request that
  lands inside a callback is not swallowed.
"""

from __future__ import annotations

import gzip
import inspect
import json
import os
import sys
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from types import CodeType, FrameType
from typing import Any

from pytest_coverset.features import object_shape_key, shape_key
from pytest_coverset.paths import is_environment_path, is_test_file, norm_path
from pytest_coverset.redact import ReprMode, example_repr

TOOL_ID_PREFERENCE = (4, 5, 2)
TOOL_NAME = "coverset"
RECEIVER = "<receiver>"
RETURN = "return"
IMPORT_CALLER = "<import>"
SHAPES_FORMAT_VERSION = 1

_CO_OPTIMIZED = inspect.CO_OPTIMIZED
_CO_VARARGS = inspect.CO_VARARGS
_CO_VARKEYWORDS = inspect.CO_VARKEYWORDS
_CO_NO_RETURN_SHAPE = inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR
_RECEIVER_NAMES = frozenset({"self", "cls"})
_LOCALS = "<locals>"
_LAMBDA = "<lambda>"
_SKIP_CALLER = -2
"""``_caller_of`` marker: a generator-expression (or comprehension) frame; the real caller is further out."""
_MAX_CALLER_HOPS = 8
_TYPE_MRO = type.__dict__["__mro__"]
_TYPE_NAME = type.__dict__["__name__"]
FULL_TRACE_CAP = 100_000
"""Default safety cap on sampled calls per full-trace code object (``--coverset-full-trace-cap``)."""


@dataclass(slots=True)
class _FuncState:
    """Sampling state of one target code object (held for the session)."""

    code: CodeType
    index: int
    params: tuple[tuple[str, str], ...]
    full: bool
    record_return: bool
    owner: str | None = None
    """Name of the class the function is defined in when its first parameter may be the receiver."""
    calls: int = 0
    since_new: int = 0
    capped: bool = False
    cap: str | None = None
    """Why sampling stopped: ``"sample"`` (adaptive cap) or ``"full"`` (full-trace safety cap)."""
    seen: set[tuple[str, int]] = field(default_factory=set)
    tests: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _Aggregate:
    samples: int
    tests: set[int]
    example: str | None
    example_test: int | None


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """Which code the tracer samples.

    ``packages`` and ``modules`` match a function's module ``__name__`` (the module itself or a
    submodule); ``paths`` are git-root-relative directories or files; ``full_trace`` files are never
    sample-capped and are always targets. When all of ``packages``, ``modules`` and ``paths`` are
    empty, every in-repo file outside virtualenv directories is a target. Test modules
    (``test_*.py``, ``*_test.py``) and ``conftest.py`` never are.
    """

    packages: tuple[str, ...] = ()
    modules: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    full_trace: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls,
        sources: Sequence[str],
        trace_modules: Sequence[str],
        full_trace_paths: Iterable[str],
        repo_root: str,
    ) -> TargetSpec:
        """Split ``--coverset-source`` values into directories/files (when they exist) and package names."""
        packages: list[str] = []
        paths: list[str] = []
        for source in sources:
            if os.path.exists(source):
                rel = norm_path(source, repo_root)
                if rel is not None:
                    paths.append(rel)
            else:
                packages.append(source)
        full = {_repo_relative(p, repo_root) for p in full_trace_paths}
        return cls(tuple(packages), tuple(trace_modules), tuple(paths), frozenset(p for p in full if p))

    def matches(self, rel_path: str, module_name: str | None) -> bool:
        """True when a function defined in ``rel_path`` (module ``module_name``) should be sampled."""
        if not (self.packages or self.modules or self.paths):
            return not is_environment_path(rel_path)
        if any(rel_path == p or rel_path.startswith(p + "/") for p in self.paths):
            return True
        if module_name is None:
            return False
        return any(
            module_name == m or module_name.startswith(m + ".") for m in (*self.packages, *self.modules)
        )


def _repo_relative(path: str, repo_root: str) -> str | None:
    path = path.strip()
    if not path:
        return None
    if os.path.isabs(path):
        return norm_path(path, repo_root)
    return Path(path).as_posix().removeprefix("./")


def read_full_trace_file(path: str | os.PathLike[str]) -> list[str]:
    """Read a ``--coverset-full-trace`` file: one path per line, blank lines ignored."""
    return [line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


class ShapeTracer:
    """Records argument and return-value shapes of target functions through ``sys.monitoring``."""

    def __init__(
        self,
        *,
        repo_root: str,
        targets: TargetSpec,
        repr_mode: ReprMode = "scalars",
        max_samples: int = 2048,
        settle: int = 64,
        full_cap: int = FULL_TRACE_CAP,
        tool_ids: Sequence[int] = TOOL_ID_PREFERENCE,
    ) -> None:
        self.repo_root = repo_root
        self.targets = targets
        self.repr_mode: ReprMode = repr_mode
        self.max_samples = max_samples
        self.settle = settle
        self.full_cap = full_cap
        self._tool_ids = tuple(tool_ids)
        self.tool_id: int | None = None
        self.disabled_reason: str | None = None
        self.tracer_errors = 0
        self.callback_seconds = 0.0
        self.events_seen = 0
        self._local = threading.local()
        self._paths: dict[str, str | None] = {}
        self._targets: dict[int, _FuncState] = {}
        self._caller_of: dict[int, tuple[CodeType, int]] = {}
        self._funcs: list[_FuncState] = []
        self._shape_index: dict[str, int] = {}
        self._shapes: list[str] = []
        self._test_index: dict[str, int] = {}
        self._tests: list[str] = []
        self._caller_index: dict[tuple, int] = {}
        self._callers: list[dict[str, Any]] = []
        self._agg: dict[tuple[int, str, int, int], _Aggregate] = {}
        self._current_test = -1
        self._current_test_caller = self._intern_caller(("test", IMPORT_CALLER))
        self._active = False
        self._registered = False

    def start(self) -> bool:
        """Claim a tool id and enable discovery; False (with :attr:`disabled_reason`) when not possible."""
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
        monitoring.register_callback(self.tool_id, events.PY_START, self._on_start)
        monitoring.register_callback(self.tool_id, events.PY_RETURN, self._on_return)
        self._registered = True
        monitoring.set_events(self.tool_id, events.PY_START)
        self._active = True
        return True

    def set_current_test(self, nodeid: str | None) -> None:
        """Attribute subsequent calls to ``nodeid`` (None between tests)."""
        if nodeid is None:
            self._current_test = -1
            self._current_test_caller = self._intern_caller(("test", IMPORT_CALLER))
            return
        index = self._test_index.get(nodeid)
        if index is None:
            index = self._test_index[nodeid] = len(self._tests)
            self._tests.append(nodeid)
        self._current_test = index
        self._current_test_caller = self._intern_caller(("test", nodeid))

    def stop(self) -> None:
        """Switch every event of this tool off (global and local); recorded data is kept. Idempotent."""
        if not self._active or self.tool_id is None:
            return
        self._active = False
        monitoring = sys.monitoring
        monitoring.set_events(self.tool_id, 0)
        for state in self._funcs:
            if not state.capped:
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

    @property
    def functions_traced(self) -> int:
        """Number of target code objects discovered so far."""
        return len(self._funcs)

    @property
    def functions_truncated(self) -> int:
        """Number of target code objects whose sampling was capped."""
        return sum(1 for state in self._funcs if state.capped)

    def _on_start(self, code: CodeType, offset: int) -> object:
        local = self._local
        if getattr(local, "busy", False):
            return None
        state = self._targets.get(id(code))
        if state is not None and state.capped:
            return sys.monitoring.DISABLE
        started = perf_counter()
        local.busy = True
        try:
            self.events_seen += 1
            frame = sys._getframe(1)
            if frame.f_code is not code:
                return None
            if state is None:
                state = self._discover(code, frame)
                if state is None:
                    return sys.monitoring.DISABLE
            else:
                cap = self._cap_reached(state)
                if cap is not None:
                    state.capped, state.cap = True, cap
                    sys.monitoring.set_local_events(self.tool_id, code, 0)
                    return sys.monitoring.DISABLE
            self._sample_call(state, frame)
        except KeyboardInterrupt:
            raise
        except BaseException:
            self.tracer_errors += 1
        finally:
            local.busy = False
            self.callback_seconds += perf_counter() - started
        return None

    def _cap_reached(self, state: _FuncState) -> str | None:
        if state.full:
            return "full" if state.calls >= self.full_cap else None
        if state.calls >= self.max_samples or state.since_new >= self.settle:
            return "sample"
        return None

    def _on_return(self, code: CodeType, offset: int, retval: object) -> object:
        local = self._local
        if getattr(local, "busy", False):
            return None
        state = self._targets.get(id(code))
        if state is None or state.capped or not state.record_return:
            return sys.monitoring.DISABLE
        started = perf_counter()
        local.busy = True
        try:
            self.events_seen += 1
            frame = sys._getframe(1)
            caller = self._caller(frame.f_back if frame.f_code is code else None)
            if self._observe(state, RETURN, shape_key(retval), caller, retval):
                state.since_new = 0
        except KeyboardInterrupt:
            raise
        except BaseException:
            self.tracer_errors += 1
        finally:
            local.busy = False
            self.callback_seconds += perf_counter() - started
        return None

    def _rel_path(self, filename: str) -> str | None:
        try:
            return self._paths[filename]
        except KeyError:
            rel = norm_path(filename, self.repo_root)
            if rel is not None or not filename.startswith("<"):
                # Pseudo file names (``<string>``, ``<gen 17>`` from exec/compile) are not cached:
                # code generated at run time could otherwise grow this map without bound.
                self._paths[filename] = rel
            return rel

    def _discover(self, code: CodeType, frame: FrameType) -> _FuncState | None:
        state: _FuncState | None = None
        rel = self._rel_path(code.co_filename)
        if (
            rel is not None
            and not is_test_file(rel)
            and code.co_flags & _CO_OPTIMIZED
            and (not code.co_name.startswith("<") or code.co_name == _LAMBDA)
        ):
            full = rel in self.targets.full_trace
            if full or self.targets.matches(rel, _module_name(frame)):
                state = self._new_state(code, full)
        if state is not None:
            # Only targets are remembered (the state keeps the code object alive, so its id stays
            # unique). A non-target gets DISABLE and is forgotten: CPython stops reporting it.
            self._targets[id(code)] = state
            events = sys.monitoring.events
            wanted = events.PY_START | (events.PY_RETURN if state.record_return else 0)
            sys.monitoring.set_local_events(self.tool_id, code, wanted)
        return state

    def _new_state(self, code: CodeType, full: bool) -> _FuncState:
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
        state = _FuncState(
            code=code,
            index=len(self._funcs),
            params=tuple(params),
            full=full,
            record_return=not code.co_flags & _CO_NO_RETURN_SHAPE,
            owner=owner,
        )
        self._funcs.append(state)
        return state

    def _caller(self, frame: FrameType | None) -> int:
        """The caller index for a call made from ``frame``.

        Generator-expression (and, before 3.12, comprehension) frames are skipped: the caller is the
        function that iterates them. Module and class bodies (``<module>``, import-time calls) and
        anything outside the repository, in a test file or in a virtualenv map to the running test,
        or ``<import>`` outside any test.
        """
        for _ in range(_MAX_CALLER_HOPS):
            if frame is None:
                break
            code = frame.f_code
            known = self._caller_of.get(id(code))
            if known is None:
                known = self._caller_of[id(code)] = (code, self._classify_caller(code))
            if known[1] == _SKIP_CALLER:
                frame = frame.f_back
                continue
            return known[1] if known[1] >= 0 else self._current_test_caller
        return self._current_test_caller

    def _classify_caller(self, code: CodeType) -> int:
        if not code.co_flags & _CO_OPTIMIZED:
            return -1  # module or class body
        name = code.co_name
        if name.startswith("<") and name != _LAMBDA:
            return _SKIP_CALLER  # <genexpr>, <listcomp>, <setcomp>, <dictcomp>
        rel = self._rel_path(code.co_filename)
        if rel is None or is_test_file(rel) or is_environment_path(rel):
            return -1
        return self._intern_caller(("func", rel, code.co_qualname, code.co_firstlineno))

    def _intern_caller(self, key: tuple) -> int:
        index = self._caller_index.get(key)
        if index is None:
            index = self._caller_index[key] = len(self._callers)
            if key[0] == "func":
                self._callers.append({"kind": "func", "path": key[1], "qualname": key[2], "line": key[3]})
            else:
                self._callers.append({"kind": "test", "nodeid": key[1]})
        return index

    def _sample_call(self, state: _FuncState, frame: FrameType) -> None:
        state.calls += 1
        if self._current_test >= 0:
            state.tests.add(self._current_test)
        caller = self._caller(frame.f_back)
        values = frame.f_locals
        snapshot = type(values) is dict
        new = False
        try:
            for index, (name, label) in enumerate(state.params):
                try:
                    value = values[name]
                except KeyError:
                    continue
                try:
                    receiver = _receiver_class(value, state.owner) if index == 0 and state.owner else None
                    if receiver is not None:
                        key = object_shape_key(receiver)
                        new = self._observe(state, RECEIVER, key, caller, None) or new
                    else:
                        new = self._observe(state, label, shape_key(value), caller, value) or new
                except KeyboardInterrupt:
                    raise
                except Exception:
                    self.tracer_errors += 1
                finally:
                    value = None
        finally:
            if snapshot:
                # Python 3.12: f_locals is a dict snapshot cached on the frame. Left filled, it would
                # keep every argument alive until the function returns (a `del arg` inside the
                # function would not free it). Nothing in the function has run yet at PY_START, and
                # any later f_locals/locals() call refreshes the snapshot, so clearing is invisible.
                values.clear()
        state.since_new = 0 if new else state.since_new + 1

    def _observe(self, state: _FuncState, label: str, key: str, caller: int, value: object) -> bool:
        shape = self._shape_index.get(key)
        if shape is None:
            shape = self._shape_index[key] = len(self._shapes)
            self._shapes.append(key)
        agg_key = (state.index, label, shape, caller)
        agg = self._agg.get(agg_key)
        test = self._current_test
        if agg is None:
            agg = self._agg[agg_key] = _Aggregate(0, set(), None, test if test >= 0 else None)
            if label != RECEIVER:
                agg.example = self._example(state, label, value)
        agg.samples += 1
        if test >= 0:
            agg.tests.add(test)
        seen_key = (label, shape)
        if seen_key in state.seen:
            return False
        state.seen.add(seen_key)
        return True

    def _example(self, state: _FuncState, label: str, value: object) -> str | None:
        try:
            return example_repr(label, value, self.repr_mode, func_name=state.code.co_name)
        except Exception:
            self.tracer_errors += 1
            return None

    def write(self, path: str | os.PathLike[str]) -> dict[str, int]:
        """Write ``shapes.jsonl.gz`` (Addendum B) and return row counts."""
        funcs = []
        for state in self._funcs:
            rel = self._rel_path(state.code.co_filename)
            funcs.append([rel, state.code.co_qualname, state.code.co_firstlineno])
        header = {
            "kind": "header",
            "version": SHAPES_FORMAT_VERSION,
            "funcs": funcs,
            "shapes": self._shapes,
            "tests": self._tests,
            "callers": self._callers,
        }
        n_obs = 0
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as out:
            out.write(json.dumps(header, separators=(",", ":")) + "\n")
            for (f, param, shape, caller), agg in self._agg.items():
                row = {
                    "kind": "obs",
                    "f": f,
                    "p": param,
                    "s": shape,
                    "c": caller,
                    "n": agg.samples,
                    "t": sorted(agg.tests),
                    "trunc": self._funcs[f].capped,
                    "ex": agg.example,
                    "ext": agg.example_test,
                }
                out.write(json.dumps(row, separators=(",", ":")) + "\n")
                n_obs += 1
            for state in self._funcs:
                row = {
                    "kind": "func_sampling",
                    "f": state.index,
                    "calls": state.calls,
                    "tests": sorted(state.tests),
                    "truncated": state.capped,
                    "cap": state.cap,
                }
                out.write(json.dumps(row, separators=(",", ":")) + "\n")
        return {"funcs": len(self._funcs), "shapes": len(self._shapes), "obs": n_obs}


def _module_name(frame: FrameType) -> str | None:
    name = frame.f_globals.get("__name__")
    return name if type(name) is str else None


def _owner_class(code: CodeType) -> str | None:
    """The defining class's name when ``code`` is a function defined directly in a class body.

    ``A.method`` and ``outer.<locals>.A.method`` have an owner (``A``); a module-level function, a
    nested function (``outer.<locals>.inner``) and a lambda do not.
    """
    parent, dot, name = code.co_qualname.rpartition(".")
    if not dot or name == _LAMBDA:
        return None
    owner = parent.rpartition(".")[2]
    return None if owner == _LOCALS or owner.startswith("<") else owner


def _receiver_class(value: object, owner: str) -> type | None:
    """The class to record for ``self``/``cls``, or None when ``value`` is not an ``owner`` receiver.

    ``type(value)`` and type's own ``__mro__``/``__name__`` descriptors only: ``isinstance`` would
    consult a user ``__class__`` and a metaclass could override attribute lookup. A ``@staticmethod``
    whose first parameter is named ``self`` gets an unrelated value and is recorded as a normal
    parameter.
    """
    cls = type(value)
    receiver = value if issubclass(cls, type) else cls
    for klass in _TYPE_MRO.__get__(receiver):
        if _TYPE_NAME.__get__(klass) == owner:
            return receiver  # type: ignore[return-value]
    return None
