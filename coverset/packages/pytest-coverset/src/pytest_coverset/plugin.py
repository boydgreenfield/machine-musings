"""pytest entry point of pytest-coverset.

Installing the plugin changes nothing about a normal pytest run: it only registers the
``invariant(id)`` marker. Instrumentation (per-test coverage, the shape tracer, JSON-line events and
the facts bundle) switches on with ``--coverset-out DIR``, or with ``--coverset`` (``COVERSET=1``),
which adds local defaults (:mod:`pytest_coverset.local`); priority ordering switches on with
``--coverset-scores FILE``.

Every path the plugin writes is repository-relative (the git root, else the rootdir), whichever
directory pytest runs in; ``meta.cwd`` and ``meta.rootdir`` say where the run happened, relative to
the same root, and ``meta.coverage_paths`` says whether ``coverage.json``/``coverage.sqlite`` names
were rewritten (``repository``) or are still coverage.py's, relative to ``meta.cwd`` (``cwd``).

Stopping: the runner either creates the ``--coverset-stop-file`` (checked before each test) or sends
SIGTERM. While instrumentation is on, SIGTERM is turned into a ``KeyboardInterrupt`` (once), so a
hung test is interrupted, pytest exits with code 2 and ``pytest_sessionfinish`` still writes the
bundle; the interrupted test counts as ``not_run``. Further SIGTERMs while the bundle is written are
ignored, so the runner should wait a few seconds before escalating to SIGKILL. Instrumentation never
aborts the test run: errors while recording a result or emitting an event go to ``meta.errors``.

With ``--coverset-neutral`` (or ``COVERSET_NEUTRAL=1``) the language-neutral bundle is written as
well, on every path that writes the Python bundle (:mod:`pytest_coverset.neutral`).
"""

from __future__ import annotations

import sys

import pytest

if sys.version_info < (3, 12):  # noqa: UP036 - a clear error instead of a TypeError on 3.11
    raise pytest.UsageError(
        "pytest-coverset needs Python 3.12 or later (the shape tracer uses sys.monitoring); "
        f"this is Python {sys.version.split()[0]}. Uninstall pytest-coverset from this environment "
        "or run the tests on 3.12+."
    )

import argparse
import json
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
from pytest_coverset.local import (
    DEFAULT_OUT,
    display_path,
    pop_switch,
    prepare_out_dir,
    sources_from_pyproject,
    summary_line,
)
from pytest_coverset.neutral import NeutralWriter, env_enabled, remove_previous, shard_from_env
from pytest_coverset.ordering import PriorityOrdering, load_scores
from pytest_coverset.outcomes import exception_name, fold_reports, truncate_utf8
from pytest_coverset.paths import coverage_name, find_git_root, norm_path, rel_dir
from pytest_coverset.redact import ReprMode
from pytest_coverset.shapes import FULL_TRACE_CAP, ShapeTracer, TargetSpec, read_full_trace_file

SESSION_PLUGIN = "coverset-session"
ORDERING_PLUGIN = "coverset-ordering"
STOP_REASON = "coverset: stop requested"
SIGTERM_REASON = "coverset: SIGTERM received"
INVARIANT_MARKER = "invariant(id): link this test to the invariant `id` declared in invariants.yaml"
ENV_SWITCH = pytest.StashKey[bool]()
"""Whether ``COVERSET=1`` was in the environment (read, and removed, once per config)."""


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--coverset-*`` options."""
    group = parser.getgroup(
        "coverset", "coverset instrumentation (inert unless --coverset or --coverset-out is given)"
    )
    group.addoption(
        "--coverset",
        action="store_true",
        default=False,
        help=f"instrument this run with local defaults: the bundle in <rootdir>/{DEFAULT_OUT}, sources "
        "from [tool.coverset] in <rootdir>/pyproject.toml, the neutral export, a summary line; "
        "COVERSET=1 does the same",
    )
    group.addoption(
        "--coverset-out", metavar="DIR", default=None, help="write the coverset facts bundle to DIR"
    )
    group.addoption(
        "--coverset-neutral",
        action="store_true",
        default=False,
        help="also write the language-neutral bundle (DIR/neutral, DIR/neutral.tar.gz); "
        "COVERSET_NEUTRAL=1 does the same",
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
    group.addoption(
        "--coverset-full-trace-cap",
        type=_positive_int,
        default=FULL_TRACE_CAP,
        metavar="N",
        help="safety cap on sampled calls per function in a --coverset-full-trace file",
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
    full_trace_cap: int
    neutral: bool = False
    local: bool = False
    """``--coverset``: defaults came from the project, and a summary line is printed at the end."""

    @classmethod
    def from_namespace(cls, ns: Any, *, local: bool = False, rootdir: Path | None = None) -> Options:
        """Build from an argparse namespace (``early_config.known_args_namespace`` or ``config.option``).

        With ``local`` (``--coverset``), an option left unset takes its local default: the output
        directory ``<rootdir>/.coverset``, the sources of ``[tool.coverset]`` (a usage error when
        there are none), the neutral export on.
        """
        sources = tuple(ns.coverset_source or ())
        if ns.coverset_out:
            out_dir = Path(ns.coverset_out).absolute()
        elif local and rootdir is not None:
            out_dir = Path(rootdir).absolute() / DEFAULT_OUT
        else:
            raise pytest.UsageError("coverset: no output directory (pass --coverset-out=DIR or --coverset)")
        if local and not sources:
            sources = sources_from_pyproject(Path(rootdir) if rootdir is not None else Path.cwd())
        return cls(
            out_dir=out_dir,
            sources=sources,
            trace_modules=tuple(ns.coverset_trace_module or ()),
            full_trace_file=ns.coverset_full_trace,
            shapes=ns.coverset_shapes == "on",
            repr_mode=ns.coverset_repr_examples,
            stop_file=ns.coverset_stop_file,
            shape_samples=ns.coverset_shape_samples,
            shape_settle=ns.coverset_shape_settle,
            full_trace_cap=ns.coverset_full_trace_cap,
            neutral=bool(getattr(ns, "coverset_neutral", False)) or env_enabled() or local,
            local=local,
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
        prepare_out_dir(options.out_dir)
        self.start_cwd = os.getcwd()
        self.cwd_rel = rel_dir(self.start_cwd, self.repo_root)
        """The working directory relative to the repository root (``.``), None when outside it."""
        self.rootdir_rel = rel_dir(config.rootpath, self.repo_root)
        self.coverage_paths = "cwd"
        """``repository`` once coverage.json and coverage.sqlite names are repository-relative."""
        self.files_measured: int | None = None
        self.shard = shard_from_env()
        self.neutral = (
            NeutralWriter(options.out_dir, self.repo_root, self.start_cwd) if options.neutral else None
        )
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
            full_cap=self.options.full_trace_cap,
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
        # The event does not depend on the file write: a failed write is an error in meta.json,
        # while the row stays in memory for the bundle and the runner still sees the result.
        self._attempt(f"results.jsonl {item.nodeid}", lambda: self.results.add(result))
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
        self._attempt("coverage paths", self._repository_paths)
        if self.tracer is not None:
            self._attempt("shapes", lambda: self.tracer.write(out / SHAPES_FILE))  # type: ignore[union-attr]
        else:
            self._attempt("shapes", lambda: write_empty_shapes(out / SHAPES_FILE))
        self.results.close()
        counts = self.results.counts(len(self.collected))
        for name, exc in write_static_files(out, self.collected, self.collect_errors, self.results.results):
            self._record_error(name, exc)
        self._note_tracer_loss()
        neutral = self.neutral
        meta = self._meta(exit_code, counts)
        # The Python bundle is complete (meta.json, then bundle.tar.gz) before the neutral export
        # starts, so an interrupt during the export never costs it; meta.json is rewritten with
        # the export's outcome afterwards.
        self._attempt("meta", lambda: write_json(out / META_FILE, meta))
        try:
            path, digest, size = make_tarball(out)
        except Exception as exc:
            self._record_error("tarball", exc)
        else:
            self._emit("bundle_written", path=str(path), sha256=digest, bytes=size)
        try:
            if neutral is not None:
                neutral.name_prefix = self._name_prefix()
                self._export_neutral(neutral, meta)
            else:
                self._attempt("neutral cleanup", lambda: remove_previous(out))
            self._emit("done", exit_code=exit_code, counts=counts)
        finally:
            if self.options.local:
                self._attempt("summary", self._print_summary)

    def _repository_paths(self) -> None:
        """Make every ``coverage.json``/``coverage.sqlite`` file name repository-relative."""
        cwd, root = self.start_cwd, self.repo_root
        self.files_measured = self.coverage.remap_files(lambda name: coverage_name(name, cwd, root))
        self.coverage_paths = "repository"

    def _name_prefix(self) -> str:
        """What the neutral export joins in front of coverage names (the converter's rule)."""
        if self.coverage_paths == "repository" or self.cwd_rel in (None, "."):
            return ""
        return f"{self.cwd_rel}/"

    def _print_summary(self) -> None:
        """``--coverset``'s one line: where the bundle went, how much it holds, how to read it."""
        reporter = self.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is None:
            return
        out = display_path(self.options.out_dir, self.start_cwd, self.repo_root)
        files = self.files_measured
        if files is None:  # the rewrite failed: count coverage.json's relative names instead
            try:
                report = json.loads(self.coverage.json_file.read_text(encoding="utf-8"))
                files = sum(1 for name in report["files"] if not os.path.isabs(name))
            except (OSError, ValueError, KeyError, TypeError):
                files = 0
        line = summary_line(out, len(self.results.results), files, len(self.errors))
        reporter.ensure_newline()
        reporter.write_line(line)

    def _export_neutral(self, neutral: NeutralWriter, meta: dict[str, Any]) -> None:
        """Write the neutral files, manifest and archive; record the outcome in ``meta.json``.

        ``meta.neutral.status`` is ``written`` (``neutral_written`` emitted), ``failed`` (the errors
        are in ``meta.errors`` under ``neutral ...``; no manifest is written over incomplete data
        files and no archive or event is produced) or ``interrupted`` (the export was cut short,
        e.g. Ctrl-C; the interrupt is re-raised after ``meta.json`` is rewritten).
        """
        out = self.options.out_dir
        outcome: dict[str, Any] = {"requested": True, "status": "failed"}
        try:
            data_ok = self._attempt(
                "neutral",
                lambda: neutral.write_data(
                    self.collected,
                    self.results.results,
                    self.coverage.json_file,
                    self.coverage.data_file,
                    out / SHAPES_FILE,
                ),
            )
            manifest_meta = {**meta, "neutral": {"requested": True, **neutral.outcome()}}
            manifest_ok = data_ok and self._attempt(
                "neutral manifest",
                lambda: neutral.write_manifest(manifest_meta, version=__version__, shard=self.shard),
            )
            packed: tuple[Path, str, int] | None = None
            if manifest_ok:
                try:
                    packed = neutral.pack()
                except Exception as exc:
                    self._record_error("neutral tarball", exc)
            outcome.update(neutral.outcome())
            if packed is not None:
                path, digest, size = packed
                outcome.update(status="written", path=str(path), sha256=digest, bytes=size)
                self._emit(
                    "neutral_written",
                    path=str(path),
                    sha256=digest,
                    bytes=size,
                    seconds=outcome["seconds"],
                    rows=outcome["rows"],
                    dropped=outcome["dropped"],
                )
        except BaseException:
            outcome.update(neutral.outcome(), status="interrupted")
            raise
        finally:
            meta["neutral"] = outcome
            self._attempt("meta", lambda: write_json(out / META_FILE, meta))

    def abort(self, exit_code: int, label: str, exc: BaseException) -> None:
        """Finalise after pytest gave up before the session ran (a conftest failed to import, ...).

        The bundle is still written, with ``label``'s error in ``meta.errors``, and ``done`` carries
        ``exit_code``, so the runner sees a finished shard instead of silence.
        """
        self._record_error(label, exc)
        try:
            self.finalize(exit_code)
        finally:
            self.shutdown()

    def _note_tracer_loss(self) -> None:
        cov = self.coverage
        if cov.tracer_lost_at is None:
            return
        first = cov.tracer_losses[0] if cov.tracer_losses else {}
        repaired = "re-installed" if first.get("reinstalled") else "not re-installed"
        message = (
            f"coverage tracer lost: during {cov.tracer_lost_at} the trace function became "
            f"{first.get('found', '?')} ({repaired}); {cov.tracer_lost_count} context switch(es) affected"
        )
        if message not in self.errors:
            self.errors.append(message)

    def _attempt(self, label: str, action: Callable[[], object]) -> bool:
        """Run ``action``; an exception is recorded under ``label``. Returns whether it succeeded."""
        try:
            action()
        except Exception as exc:
            self._record_error(label, exc)
            return False
        return True

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
            "tracer_lost_at": self.coverage.tracer_lost_at,
            "tracer_losses": self.coverage.tracer_losses,
            "coverage_warnings": self.coverage.warnings,
            "context_format": "nodeid",
            "tool_id": tracer.tool_id if tracer is not None else None,
            "git_root": str(self.git_root) if self.git_root is not None else None,
            "repo_root": self.repo_root,
            "rootdir": self.rootdir_rel,
            "configfile": str(self.config.inipath) if self.config.inipath is not None else None,
            "cwd": self.cwd_rel,
            "coverage_paths": self.coverage_paths,
            "sources": list(self.options.sources),
            "trace_modules": list(self.options.trace_modules),
            "full_trace": list(self.full_trace),
            "shapes": "on" if self.options.shapes else "off",
            "shapes_disabled_reason": disabled_reason,
            "repr_examples": self.options.repr_mode,
            "shape_samples": self.options.shape_samples,
            "shape_settle": self.options.shape_settle,
            "full_trace_cap": self.options.full_trace_cap,
            "neutral": {"requested": True, "status": "pending"}
            if self.options.neutral
            else {"requested": False},
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


def _local(config: pytest.Config, namespace: Any) -> bool:
    """``--coverset`` or ``COVERSET=1`` (the variable is read, and removed, once per config)."""
    if getattr(namespace, "coverset", False):
        return True
    if ENV_SWITCH not in config.stash:
        config.stash[ENV_SWITCH] = pop_switch()
    return config.stash[ENV_SWITCH]


def _wanted(config: pytest.Config, namespace: Any) -> bool:
    """Whether this run is instrumented: ``--coverset-out``, ``--coverset`` or ``COVERSET=1``."""
    local = _local(config, namespace)
    return bool(getattr(namespace, "coverset_out", None)) or local


def _start_session(config: pytest.Config, namespace: Any) -> None:
    if config.pluginmanager.has_plugin(SESSION_PLUGIN):
        return
    _refuse_xdist(namespace)
    options = Options.from_namespace(namespace, local=_local(config, namespace), rootdir=config.rootpath)
    session = CoversetSession(config, options)
    session.start()
    config.pluginmanager.register(session, SESSION_PLUGIN)


def _refuse_xdist(namespace: Any) -> None:
    """``--coverset-out`` with xdist workers would write an empty bundle: refuse it up front."""
    workers = getattr(namespace, "numprocesses", None)
    if workers not in (None, 0, "0"):
        raise pytest.UsageError(
            f"--coverset-out cannot be combined with pytest-xdist (-n {workers}): per-test coverage, "
            "shapes and results are recorded in this process only. Run with -n 0 "
            "(coverset shards the suite across workers itself)."
        )


@pytest.hookimpl(wrapper=True, specname="pytest_load_initial_conftests")
def pytest_load_initial_conftests_guard(
    early_config: pytest.Config, parser: pytest.Parser, args: list[str]
) -> Generator[None]:
    """When loading the initial conftests fails, pytest exits (4) without a session: write the bundle."""
    try:
        return (yield)
    except BaseException as exc:
        namespace = early_config.known_args_namespace
        session = early_config.pluginmanager.get_plugin(SESSION_PLUGIN)
        if isinstance(session, CoversetSession) and not (
            getattr(namespace, "help", False) or getattr(namespace, "version", False)
        ):
            code = (
                pytest.ExitCode.USAGE_ERROR
                if isinstance(exc, (pytest.UsageError, ImportError))
                or type(exc).__name__ == "ConftestImportFailure"
                else pytest.ExitCode.INTERNAL_ERROR
            )
            session.abort(int(code), "loading conftest files", exc)
            early_config.pluginmanager.unregister(session, SESSION_PLUGIN)
        raise


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(
    early_config: pytest.Config, parser: pytest.Parser, args: list[str]
) -> None:
    """Start instrumentation before conftest files are imported, so their imports are measured."""
    namespace = early_config.known_args_namespace
    if _wanted(early_config, namespace):
        _start_session(early_config, namespace)


def pytest_configure(config: pytest.Config) -> None:
    """Register the invariant marker; enable ordering and (if not yet started) instrumentation."""
    config.addinivalue_line("markers", INVARIANT_MARKER)
    if _wanted(config, config.option):
        _refuse_xdist(config.option)
        _start_session(config, config.option)
    scores_file = config.option.coverset_scores
    if scores_file and not config.pluginmanager.has_plugin(ORDERING_PLUGIN):
        try:
            scores = load_scores(scores_file)
        except (OSError, ValueError) as exc:
            raise pytest.UsageError(f"--coverset-scores: cannot read {scores_file}: {exc}") from exc
        config.pluginmanager.register(PriorityOrdering(scores), ORDERING_PLUGIN)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Tear down the shape tracer (free the tool id) and coverage.

    When ``pytest_sessionfinish`` never ran (a later ``pytest_configure`` raised a UsageError, ...),
    the bundle is finalised here with exit code 4, so the runner still gets a bundle and ``done``.
    """
    session = config.pluginmanager.get_plugin(SESSION_PLUGIN)
    if isinstance(session, CoversetSession):
        if not session.finalized:
            session._record_error("session", RuntimeError("pytest ended before the test session finished"))
            session._attempt("finalize", lambda: session.finalize(int(pytest.ExitCode.USAGE_ERROR)))
        session.shutdown()
        config.pluginmanager.unregister(session, SESSION_PLUGIN)
    ordering = config.pluginmanager.get_plugin(ORDERING_PLUGIN)
    if ordering is not None:
        config.pluginmanager.unregister(ordering, ORDERING_PLUGIN)
