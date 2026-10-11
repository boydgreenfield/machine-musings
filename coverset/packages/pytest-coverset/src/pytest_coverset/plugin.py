"""pytest entry point of pytest-coverset.

Installing the plugin changes nothing about a normal pytest run: it only registers the
``invariant(id)`` marker. Instrumentation (per-test coverage, the shape tracer, JSON-line events and
the facts bundle) switches on with ``--coverset-out DIR``; priority ordering switches on with
``--coverset-scores FILE``.

Stopping: the runner either creates the ``--coverset-stop-file`` (checked before each test) or sends
SIGTERM. While instrumentation is on, SIGTERM is turned into a ``KeyboardInterrupt`` (once), so a
hung test is interrupted, pytest exits with code 2 and ``pytest_sessionfinish`` still writes the
bundle; the interrupted test counts as ``not_run``. Further SIGTERMs while the bundle is written are
ignored, so the runner should wait a few seconds before escalating to SIGKILL. Instrumentation never
aborts the test run: errors while recording a result or emitting an event go to ``meta.errors``.
"""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import Any

import coverage
import pytest

from pytest_coverset import __version__
from pytest_coverset.bundle import (
    META_FILE,
    SHAPES_FILE,
    CollectedItem,
    ResultsLog,
    describe_item,
    hypothesis_info,
    make_tarball,
    python_info,
    write_empty_shapes,
    write_json,
    write_static_files,
)
from pytest_coverset.coverage_ctx import CoverageContext, ForeignTracerError
from pytest_coverset.events import EventWriter
from pytest_coverset.ordering import PriorityOrdering, load_scores
from pytest_coverset.outcomes import exception_name, fold_reports, truncate_utf8
from pytest_coverset.paths import find_git_root, norm_path
from pytest_coverset.redact import ReprMode
from pytest_coverset.shapes import ShapeTracer, TargetSpec, read_full_trace_file

SESSION_PLUGIN = "coverset-session"
ORDERING_PLUGIN = "coverset-ordering"
STOP_REASON = "coverset: stop requested"
SIGTERM_REASON = "coverset: SIGTERM received"
INVARIANT_MARKER = "invariant(id): link this test to the invariant `id` declared in invariants.yaml"


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--coverset-*`` options."""
    group = parser.getgroup("coverset", "coverset instrumentation (inert unless --coverset-out is given)")
    group.addoption(
        "--coverset-out", metavar="DIR", default=None, help="write the coverset facts bundle to DIR"
    )
    group.addoption(
        "--coverset-scores",
        metavar="FILE",
        default=None,
        help="JSON {nodeid: score}; run riskiest tests first",
    )
    group.addoption(
        "--coverset-source",
        metavar="PKG",
        action="append",
        default=[],
        help="package name or path to measure and trace (repeatable)",
    )
    group.addoption(
        "--coverset-trace-module",
        metavar="MOD",
        action="append",
        default=[],
        help="extra module to trace (repeatable)",
    )
    group.addoption(
        "--coverset-full-trace",
        metavar="FILE",
        default=None,
        help="file listing paths (one per line) whose functions are traced without a sample cap",
    )
    group.addoption("--coverset-shapes", choices=("on", "off"), default="on", help="record argument shapes")
    group.addoption(
        "--coverset-repr-examples",
        choices=("off", "scalars", "all"),
        default="scalars",
        help="which example values to keep (redacted)",
    )
    group.addoption(
        "--coverset-stop-file", metavar="PATH", default=None, help="stop gracefully once PATH exists"
    )
    group.addoption(
        "--coverset-shape-samples",
        type=_positive_int,
        default=2048,
        metavar="N",
        help="sampled calls per function before the shape tracer stops sampling it",
    )
    group.addoption(
        "--coverset-shape-settle",
        type=_positive_int,
        default=64,
        metavar="N",
        help="consecutive calls without a new shape before sampling of a function stops",
    )


@dataclass(frozen=True, slots=True)
class Options:
    """Parsed ``--coverset-*`` options that matter once instrumentation is on."""

    out_dir: Path
    sources: tuple[str, ...]
    trace_modules: tuple[str, ...]
    full_trace_file: str | None
    shapes: bool
    repr_mode: ReprMode
    stop_file: str | None
    shape_samples: int
    shape_settle: int

    @classmethod
    def from_namespace(cls, ns: Any) -> Options:
        """Build from an argparse namespace (``early_config.known_args_namespace`` or ``config.option``)."""
        return cls(
            out_dir=Path(ns.coverset_out).absolute(),
            sources=tuple(ns.coverset_source or ()),
            trace_modules=tuple(ns.coverset_trace_module or ()),
            full_trace_file=ns.coverset_full_trace,
            shapes=ns.coverset_shapes == "on",
            repr_mode=ns.coverset_repr_examples,
            stop_file=ns.coverset_stop_file,
            shape_samples=ns.coverset_shape_samples,
            shape_settle=ns.coverset_shape_settle,
        )


class CoversetSession:
    """Instrumentation for one pytest session: coverage, shapes, events and the bundle."""

    def __init__(self, config: pytest.Config, options: Options) -> None:
        self.config = config
        self.options = options
        self.started_wall = time.time()
        self.started = time.perf_counter()
        self.git_root = find_git_root(config.rootpath) or find_git_root(Path.cwd())
        self.repo_root = str(self.git_root or config.rootpath)
        options.out_dir.mkdir(parents=True, exist_ok=True)
        self.coverage = CoverageContext(options.out_dir, options.sources)
        self.tracer: ShapeTracer | None = None
        self.full_trace: tuple[str, ...] = ()
        self.events = EventWriter(mid_line=self._terminal_mid_line)
        self.results = ResultsLog(options.out_dir)
        self.collected: list[CollectedItem] = []
        self.collect_errors: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._reports: dict[str, list[pytest.TestReport]] = {}
        self._exc_types: dict[tuple[str, str], str] = {}
        self._skip_item = False
        self.stop_requested = False
        self.finalized = False
        self._sigterm_received = False
        self._shielded = False
        self._interrupt_pending = False
        self._previous_sigterm: Any = None
        self._sigterm_installed = False

    def start(self) -> None:
        """Start coverage (raising UsageError if another tracer is active), then the shape tracer."""
        try:
            self.coverage.start()
        except ForeignTracerError as exc:
            self.results.close()
            raise pytest.UsageError(f"--coverset-out cannot measure coverage: {exc}") from exc
        self._install_sigterm_handler()
        if not self.options.shapes:
            return
        if self.options.full_trace_file:
            self.full_trace = tuple(read_full_trace_file(self.options.full_trace_file))
        targets = TargetSpec.build(
            self.options.sources, self.options.trace_modules, self.full_trace, self.repo_root
        )
        self.tracer = ShapeTracer(
            repo_root=self.repo_root,
            targets=targets,
            repr_mode=self.options.repr_mode,
            max_samples=self.options.shape_samples,
            settle=self.options.shape_settle,
        )
        self.tracer.start()

    def _install_sigterm_handler(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        try:
            self._previous_sigterm = signal.signal(signal.SIGTERM, self._on_sigterm)
        except (OSError, ValueError):
            return
        self._sigterm_installed = True

    def _restore_sigterm_handler(self) -> None:
        if not self._sigterm_installed:
            return
        self._sigterm_installed = False
        if signal.getsignal(signal.SIGTERM) == self._on_sigterm:
            previous = self._previous_sigterm
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)

    def _on_sigterm(self, signum: int, frame: FrameType | None) -> None:
        """Interrupt the run once; deferred while a result is being recorded, ignored once finalizing."""
        if self._sigterm_received or self.finalized:
            return
        self._sigterm_received = True
        self.stop_requested = True
        if self._shielded:
            self._interrupt_pending = True
            return
        raise KeyboardInterrupt(SIGTERM_REASON)

    def _record_error(self, label: str, exc: BaseException) -> None:
        message = f"{label}: {type(exc).__name__}: {exc}"
        if message not in self.errors:
            self.errors.append(message)

    def _emit(self, kind: str, **payload: Any) -> None:
        try:
            self.events.emit(kind, **payload)
        except Exception as exc:
            self._record_error(f"event {kind}", exc)

    def _terminal_mid_line(self) -> bool:
        try:
            return self.config.get_terminal_writer().width_of_current_line > 0
        except Exception:
            return False

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            text, truncated = truncate_utf8(report.longreprtext)
            self.collect_errors.append({"nodeid": report.nodeid, "longrepr": text, "truncated": truncated})

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.collected = [describe_item(item, self.repo_root) for item in session.items]
        self._emit("collected", count=len(self.collected), errors=len(self.collect_errors))

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_protocol(
        self, item: pytest.Item, nextitem: pytest.Item | None
    ) -> Generator[None, Any, Any]:
        stop_file = self.options.stop_file
        if self.stop_requested or (stop_file and os.path.exists(stop_file)):
            self.stop_requested = True
            item.session.shouldstop = STOP_REASON
            self._skip_item = True
            try:
                return (yield)
            finally:
                self._skip_item = False
        if self.events.heartbeat_due():
            self._emit(
                "heartbeat",
                done=len(self.results.results),
                total=len(self.collected),
                elapsed_s=round(time.perf_counter() - self.started, 3),
            )
        self.coverage.switch(item.nodeid)
        if self.tracer is not None:
            self.tracer.set_current_test(item.nodeid)
        self._reports[item.nodeid] = []
        completed = False
        try:
            result = yield
            completed = True
            return result
        finally:
            if self.tracer is not None:
                self.tracer.set_current_test(None)
            if completed:
                self._record_item(item)
            else:
                self._reports.pop(item.nodeid, None)

    @pytest.hookimpl(tryfirst=True, specname="pytest_runtest_protocol")
    def pytest_runtest_protocol_stop(self, item: pytest.Item, nextitem: pytest.Item | None) -> bool | None:
        return True if self._skip_item else None

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(
        self, item: pytest.Item, call: pytest.CallInfo[None]
    ) -> Generator[None, Any, Any]:
        report = yield
        if call.excinfo is not None and report.failed:
            self._exc_types[(item.nodeid, call.when)] = exception_name(call.excinfo.type)
        return report

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        reports = self._reports.get(report.nodeid)
        if reports is not None:
            reports.append(report)

    def _record_item(self, item: pytest.Item) -> None:
        """Record ``item``'s result; never raises into pytest except for a deferred SIGTERM."""
        self._shielded = True
        try:
            self._finish_item(item)
        except Exception as exc:
            self._record_error(f"recording {item.nodeid}", exc)
        finally:
            self._shielded = False
        if self._interrupt_pending:
            self._interrupt_pending = False
            raise KeyboardInterrupt(SIGTERM_REASON)

    def _finish_item(self, item: pytest.Item) -> None:
        reports = self._reports.pop(item.nodeid, [])
        if not reports:
            return
        exc_types = {when: name for (nodeid, when), name in self._exc_types.items() if nodeid == item.nodeid}
        for when in exc_types:
            del self._exc_types[(item.nodeid, when)]
        rel = norm_path(item.path, self.repo_root)
        result = fold_reports(item.nodeid, rel or item.location[0], reports, exc_types, self.repo_root)
        self.results.add(result)
        self._emit("test", **result.as_dict())
        if result.outcome in ("failed", "error"):
            self._attempt("coverage flush", self.coverage.flush)

    @pytest.hookimpl(wrapper=True, tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session, exitstatus: int) -> Generator[None, Any, Any]:
        try:
            return (yield)
        finally:
            self.finalize(int(session.exitstatus))

    def finalize(self, exit_code: int) -> None:
        """Stop measuring, write every bundle file and the tarball, emit ``bundle_written`` and ``done``."""
        if self.finalized:
            return
        self.finalized = True
        out = self.options.out_dir
        if self.tracer is not None:
            self.tracer.stop()
        self._attempt("coverage save", self.coverage.stop)
        self._attempt("coverage json", self.coverage.write_json)
        if self.tracer is not None:
            self._attempt("shapes", lambda: self.tracer.write(out / SHAPES_FILE))  # type: ignore[union-attr]
        else:
            self._attempt("shapes", lambda: write_empty_shapes(out / SHAPES_FILE))
        self.results.close()
        counts = self.results.counts(len(self.collected))
        self._attempt(
            "static files",
            lambda: write_static_files(out, self.collected, self.collect_errors, self.results.results),
        )
        self._attempt("meta", lambda: write_json(out / META_FILE, self._meta(exit_code, counts)))
        try:
            path, digest, size = make_tarball(out)
        except Exception as exc:
            self._record_error("tarball", exc)
        else:
            self._emit("bundle_written", path=str(path), sha256=digest, bytes=size)
        self._emit("done", exit_code=exit_code, counts=counts)

    def _attempt(self, label: str, action: Callable[[], object]) -> None:
        try:
            action()
        except Exception as exc:
            self._record_error(label, exc)

    def _meta(self, exit_code: int, counts: dict[str, int]) -> dict[str, Any]:
        tracer = self.tracer
        disabled_reason = "disabled by --coverset-shapes off" if tracer is None else tracer.disabled_reason
        return {
            "schema": 1,
            "plugin_version": __version__,
            "python": python_info(),
            "coverage_version": coverage.__version__,
            "pytest_version": pytest.__version__,
            "core": "ctrace",
            "tracer": self.coverage.tracer,
            "context_format": "nodeid",
            "tool_id": tracer.tool_id if tracer is not None else None,
            "git_root": str(self.git_root) if self.git_root is not None else None,
            "repo_root": self.repo_root,
            "rootdir": str(self.config.rootpath),
            "cwd": os.getcwd(),
            "sources": list(self.options.sources),
            "trace_modules": list(self.options.trace_modules),
            "full_trace": list(self.full_trace),
            "shapes": "on" if self.options.shapes else "off",
            "shapes_disabled_reason": disabled_reason,
            "repr_examples": self.options.repr_mode,
            "shape_samples": self.options.shape_samples,
            "shape_settle": self.options.shape_settle,
            "functions_traced": tracer.functions_traced if tracer is not None else 0,
            "functions_truncated": tracer.functions_truncated if tracer is not None else 0,
            "tracer_errors": tracer.tracer_errors if tracer is not None else 0,
            "hypothesis": hypothesis_info(),
            "exit_code": exit_code,
            "counts": counts,
            "stop_requested": self.stop_requested,
            "errors": self.errors,
            "started_at": self.started_wall,
            "finished_at": time.time(),
            "timings": {
                "session_wall_s": round(time.perf_counter() - self.started, 6),
                "tracer_callback_s": round(tracer.callback_seconds, 6) if tracer is not None else 0.0,
                "tracer_events": tracer.events_seen if tracer is not None else 0,
            },
        }

    def shutdown(self) -> None:
        """Release the tool id, restore the SIGTERM handler and stop coverage. Idempotent."""
        self._restore_sigterm_handler()
        if self.tracer is not None:
            self.tracer.close()
        self.coverage.stop()
        self.results.close()


def _start_session(config: pytest.Config, namespace: Any) -> None:
    if config.pluginmanager.has_plugin(SESSION_PLUGIN):
        return
    session = CoversetSession(config, Options.from_namespace(namespace))
    session.start()
    config.pluginmanager.register(session, SESSION_PLUGIN)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: pytest.Parser, args: list[str]
) -> None:
    """Start instrumentation before conftest files are imported, so their imports are measured."""
    namespace = early_config.known_args_namespace
    if getattr(namespace, "coverset_out", None):
        _start_session(early_config, namespace)


def pytest_configure(config: pytest.Config) -> None:
    """Register the invariant marker; enable ordering and (if not yet started) instrumentation."""
    config.addinivalue_line("markers", INVARIANT_MARKER)
    if config.option.coverset_out:
        _start_session(config, config.option)
    scores_file = config.option.coverset_scores
    if scores_file and not config.pluginmanager.has_plugin(ORDERING_PLUGIN):
        try:
            scores = load_scores(scores_file)
        except (OSError, ValueError) as exc:
            raise pytest.UsageError(f"--coverset-scores: cannot read {scores_file}: {exc}") from exc
        config.pluginmanager.register(PriorityOrdering(scores), ORDERING_PLUGIN)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Tear down the shape tracer (free the tool id) and coverage."""
    session = config.pluginmanager.get_plugin(SESSION_PLUGIN)
    if isinstance(session, CoversetSession):
        session.shutdown()
        config.pluginmanager.unregister(session, SESSION_PLUGIN)
    ordering = config.pluginmanager.get_plugin(ORDERING_PLUGIN)
    if ordering is not None:
        config.pluginmanager.unregister(ordering, ORDERING_PLUGIN)
