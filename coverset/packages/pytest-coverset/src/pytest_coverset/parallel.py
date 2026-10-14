"""pytest-xdist: every worker runs the whole plugin, the controller merges their directories.

``pytest --coverset -n 4`` (or ``--coverset-out DIR -n 4``) works like a serial run and leaves one
bundle in ``DIR``. The roles:

* **Controller** (the process you started; it runs no test). It measures nothing, removes what an
  earlier run left in ``DIR/workers/``, and hands every worker its directory,
  ``DIR/workers/<gw>/``. It removes ``DIR/workers/`` only when coverset made it: the controller
  writes the marker :data:`MARKER_FILE` (the run id) into it on creation, and an existing
  ``workers/`` without the marker (a user's package when ``DIR`` is ``.``, an artifacts directory
  in CI) is a usage error, never deleted; an empty one is reused. While tests run it folds the reports xdist forwards into the ``test``
  events a runner reads (the orchestrator never passes ``-n``; this is for local and upload runs).
  At the end it merges the workers' directories (:mod:`pytest_coverset.merge`): coverage data
  files combined with every context kept, then ``coverage.json`` reported and file names made
  repository-relative once, as a serial run does; ``results.jsonl``, ``collect.json`` and
  ``shapes.jsonl.gz`` merged; then the same ``meta.json``, ``bundle.tar.gz``, neutral export and
  summary line as a serial run. ``meta.xdist`` records the workers. The worker directories are
  removed after a clean merge (when the marker still holds this run's id) and kept (for a look)
  when anything went wrong.
* **Worker** (``gw0``, ``gw1``, ...): the full plugin (coverage with its own data file and one
  context per test, the shape tracer, results, collection) into its own directory. It writes no
  tarball, no ``coverage.json`` and no neutral export, keeps coverage.py's file names, and
  finishes its files before xdist tells the controller it is done.

The worker learns its directory from the environment variable :data:`XDIST_ENV`, which the
controller sets before xdist starts the workers and each worker removes at once (so the tests'
own subprocesses never see it), rather than from ``workerinput`` alone: a worker's conftest files
are imported before ``workerinput`` exists, and coverage must already run by then to measure
their imports. The controller also puts the directory in ``workerinput["coverset_out"]`` and the
worker checks the two agree. Only local workers are supported (``-n N``, ``--tx popen``): the
merge needs every worker's files on this disk and their file names relative to this working
directory, so ``--tx ssh=...``, ``socket=...``, ``//chdir=`` and ``--rsyncdir`` are usage errors.

Scheduling is xdist's: the default ``--dist load`` spreads single tests; module- and class-scoped
fixtures are then set up once per worker that runs a test of that module, and their lines are
attributed to whichever test set them up there. ``--dist loadfile`` (or ``loadscope``) keeps a
file's tests together when fixtures are heavy and makes the attribution match a serial run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from pytest_coverset.bundle import RESULTS_FILE, SHAPES_FILE, write_jsonl
from pytest_coverset.coverage_ctx import merge_data_files
from pytest_coverset.merge import (
    WorkerOutput,
    merge_collect,
    merge_results,
    merge_shapes,
    read_workers,
)
from pytest_coverset.outcomes import TestResult, fail_signature, fold_reports, truncate_utf8
from pytest_coverset.plugin import CoversetSession, Options

XDIST_ENV = "COVERSET_XDIST"
WORKER_ENV = "PYTEST_XDIST_WORKER"
WORKERS_DIR = "workers"
MARKER_FILE = ".coverset-xdist"
"""Written into ``workers/`` by the controller that created it: only then may a run remove it."""
WORKER_INPUT_KEY = "coverset_out"
_WORKER_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_PHASES = ("setup", "call", "teardown")


@dataclass(frozen=True, slots=True)
class WorkerToken:
    """What the controller tells its workers through :data:`XDIST_ENV`."""

    out: str
    """The controller's output directory (absolute); the worker writes to ``out/workers/<gw>``."""
    local: bool
    """``--coverset`` defaults (the controller may have been switched on by ``COVERSET=1``)."""
    events: bool
    """The controller streams events: workers write theirs to ``events.jsonl`` for reference."""
    run: str
    """A fresh id per controller run (in ``meta.xdist``), so stale tokens are recognisable."""

    def dumps(self) -> str:
        """The environment value."""
        return json.dumps({"out": self.out, "local": self.local, "events": self.events, "run": self.run})

    @classmethod
    def pop(cls, environ: dict[str, str] | Any = None) -> tuple[WorkerToken, str] | None:
        """Remove :data:`XDIST_ENV` from the environment; the token and worker id in an xdist worker.

        Outside a worker (no ``PYTEST_XDIST_WORKER``, or one that is not a plain id) the variable
        is removed all the same and None is returned.
        """
        env = os.environ if environ is None else environ
        raw = env.pop(XDIST_ENV, None)
        worker = env.get(WORKER_ENV, "")
        if raw is None or not _WORKER_ID.fullmatch(worker):
            return None
        try:
            data = json.loads(raw)
            token = cls(
                out=str(data["out"]),
                local=bool(data.get("local")),
                events=bool(data.get("events")),
                run=str(data.get("run", "")),
            )
        except (ValueError, TypeError, KeyError):
            return None
        if not os.path.isabs(token.out):
            return None
        return token, worker

    def worker_dir(self, worker: str) -> Path:
        """Where worker ``worker`` writes."""
        return Path(self.out) / WORKERS_DIR / worker


def worker_options(options: Options, token: WorkerToken, worker: str) -> Options:
    """A worker's options: its own directory, no neutral export, no summary line."""
    return replace(
        options,
        out_dir=token.worker_dir(worker),
        neutral=False,
        worker=worker,
        worker_events=token.events,
    )


def distributes(config: pytest.Config) -> bool:
    """Whether xdist runs this session on workers (its distributed session is registered)."""
    return config.pluginmanager.has_plugin("dsession")


def check_specs(config: pytest.Config) -> None:
    """Refuse worker kinds whose files the controller cannot merge (remote or chdir'd workers)."""
    specs = [str(spec) for spec in (getattr(config.option, "tx", None) or ())]
    bad = [spec for spec in specs if not _local_spec(spec)]
    if getattr(config.option, "rsyncdir", None):
        bad.append("--rsyncdir")
    if bad:
        raise pytest.UsageError(
            "coverset with pytest-xdist supports local workers only (-n N or --tx popen): the "
            "controller merges every worker's files from this disk and working directory; "
            f"not supported here: {', '.join(bad)}"
        )


def _local_spec(spec: str) -> bool:
    """``popen``, ``3*popen`` or ``popen//python=...``, without ``chdir``."""
    body = spec.split("*", 1)[1] if re.match(r"^\d+\*", spec) else spec
    parts = body.split("//")
    return parts[0] == "popen" and not any(p.startswith("chdir") for p in parts[1:])


class WorkerFinisher:
    """Finishes a worker's files inside ``pytest_sessionfinish``, before xdist reports the worker done.

    The session's own wrapper would finish them after xdist's wrapper has already told the
    controller this worker is done, and the controller could then merge half-written files.
    ``workeroutput["coverset"]`` tells the controller what the worker wrote.
    """

    def __init__(self, session: CoversetSession) -> None:
        self.session = session

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        self.session.finalize(int(session.exitstatus))
        output = getattr(session.config, "workeroutput", None)
        if isinstance(output, dict):
            output["coverset"] = {
                "out": str(self.session.options.out_dir),
                "tests": len(self.session.results.results),
                "errors": len(self.session.errors),
            }

    def pytest_configure(self, config: pytest.Config) -> None:
        expected = getattr(config, "workerinput", {}).get(WORKER_INPUT_KEY)
        if expected is not None and Path(expected) != self.session.options.out_dir:
            self.session.errors.append(
                f"xdist worker directory mismatch: workerinput says {expected}, the environment "
                f"{self.session.options.out_dir}"
            )


class XdistController(CoversetSession):
    """The controller of an instrumented xdist run: no measurement, a merge at the end."""

    def __init__(self, config: pytest.Config, options: Options) -> None:
        super().__init__(config, options)
        self.token = WorkerToken(
            out=str(options.out_dir),
            local=options.local,
            events=self.events.enabled,
            run=uuid.uuid4().hex,
        )
        self.workers_dir = options.out_dir / WORKERS_DIR
        self._pending: dict[str, list[pytest.TestReport]] = {}
        self._folded: dict[str, TestResult] = {}
        self._collected_count: int | None = None
        self._nodes: dict[str, dict[str, Any]] = {}
        self.merge_seconds = 0.0
        self.workers: list[WorkerOutput] = []
        self.worker_meta: list[dict[str, Any]] = []
        self.functions_traced = 0
        self.functions_truncated = 0

    def start(self) -> None:
        """Check the worker kinds, clear stale worker directories and publish the token."""
        check_specs(self.config)
        self._claim_workers_dir()
        os.environ[XDIST_ENV] = self.token.dumps()
        self._install_sigterm_handler()

    def _claim_workers_dir(self) -> None:
        """Make ``workers/`` this run's: replace one an earlier run left, refuse anyone else's.

        Only a directory carrying :data:`MARKER_FILE` was made by coverset and may be removed; an
        empty directory is harmless to reuse. Anything else under that name belongs to the user
        (``--coverset-out=.`` in a project with a ``workers`` package) and stops the run before a
        worker starts, rather than being deleted.
        """
        workers = self.workers_dir
        if workers.is_symlink():
            raise pytest.UsageError(f"coverset: {workers} is a symlink: remove it")
        if workers.exists():
            if (workers / MARKER_FILE).is_file():
                shutil.rmtree(workers)
            elif not workers.is_dir() or any(workers.iterdir()):
                raise pytest.UsageError(
                    f"coverset: {workers} exists and was not created by coverset (no {MARKER_FILE}): "
                    "an xdist run keeps its worker directories there, so choose another --coverset-out "
                    "or move it"
                )
        workers.mkdir(parents=True, exist_ok=True)
        (workers / MARKER_FILE).write_text(self.token.run + "\n", encoding="utf-8")

    def _owns_workers_dir(self) -> bool:
        """Whether ``workers/`` is still the directory this run created (its marker holds our id)."""
        if self.workers_dir.is_symlink() or not self.workers_dir.is_dir():
            return False
        try:
            return (self.workers_dir / MARKER_FILE).read_text(encoding="utf-8").strip() == self.token.run
        except OSError:
            return False

    # -- xdist hooks (optional: they exist only when xdist is installed) --------------------------

    @pytest.hookimpl(optionalhook=True)
    def pytest_configure_node(self, node: Any) -> None:
        node.workerinput[WORKER_INPUT_KEY] = str(self.token.worker_dir(node.gateway.id))

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: object | None) -> None:
        output = getattr(node, "workeroutput", None) or {}
        self._nodes[node.gateway.id] = {
            "finished": "coverset" in output,
            "error": None if error is None else str(error),
        }

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node: Any, ids: Sequence[str]) -> None:
        if self._collected_count is None:
            self._collected_count = len(ids)
            self._emit("collected", count=len(ids), errors=len(self.collect_errors))

    # -- results from forwarded reports ----------------------------------------------------------

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        """Fold each test's forwarded reports into a result row (events; crash fallback)."""
        reports = self._pending.setdefault(report.nodeid, [])
        reports.append(report)
        if report.when in ("setup", "call"):
            return
        self._pending.pop(report.nodeid, None)
        try:
            result = self._fold(report.nodeid, reports)
        except Exception as exc:
            self._record_error(f"folding {report.nodeid}", exc)
            return
        self._folded[report.nodeid] = result
        if self.events.enabled:
            if self.events.heartbeat_due():
                self._emit(
                    "heartbeat",
                    done=len(self._folded),
                    total=self._collected_count or 0,
                    elapsed_s=round(time.perf_counter() - self.started, 3),
                )
            self._emit("test", **result.as_dict())

    def _fold(self, nodeid: str, reports: list[pytest.TestReport]) -> TestResult:
        last = reports[-1]
        location = last.location[0] if last.location else nodeid.split("::", 1)[0]
        file = self._rel(self.config.rootpath / location) or location
        if last.when in _PHASES:
            exc_types = {
                r.when: r.coverset_exc for r in reports if isinstance(getattr(r, "coverset_exc", None), str)
            }
            return fold_reports(
                nodeid, file, [r for r in reports if r.when in _PHASES], exc_types, self.repo_root
            )
        # xdist's report for a test whose worker died ("???" phase): the test failed in its call.
        longrepr, truncated = truncate_utf8(last.longreprtext)
        return TestResult(
            nodeid=nodeid,
            file=file,
            outcome="failed",
            duration_s=round(sum(r.duration for r in reports), 6),
            failed_phase="call",
            fail_sig=fail_signature("xdist.WorkerCrashed", file, None),
            longrepr=longrepr,
            truncated=truncated,
            reason=None,
            start=None,
            stop=None,
        )

    # -- the merge ---------------------------------------------------------------------------------

    def _measurements(self) -> None:
        """Merge the worker directories into ``coverage.sqlite``, ``coverage.json``, shapes, results."""
        started = time.perf_counter()
        try:
            self._merge(read_workers(self.workers_dir))
        except Exception as exc:  # whatever was merged before stays; the bundle is still written
            self._record_error("xdist merge", exc)
        finally:
            self.merge_seconds = time.perf_counter() - started
            self.phase_seconds["merge"] = self.merge_seconds

    def _merge(self, workers: list[WorkerOutput]) -> None:
        out = self.options.out_dir
        self.workers = workers
        self.worker_meta = [w.meta or {} for w in workers]
        for worker in workers:
            for problem in worker.problems:
                self._note_error(f"xdist worker {worker.id}: {problem}")
            meta = worker.meta or {}
            for message in meta.get("errors") or ():
                self._note_error(f"xdist worker {worker.id}: {message}")
            for message in meta.get("warnings") or ():
                if message not in self.warnings:
                    self.warnings.append(str(message))
            for message in meta.get("coverage_warnings") or ():
                self.coverage._note_warning(str(message))
            worker_cwd = (meta.get("xdist") or {}).get("cwd_abs")
            if worker_cwd and os.path.realpath(worker_cwd) != os.path.realpath(self.start_cwd):
                self._note_error(
                    f"xdist worker {worker.id}: ran in {worker_cwd}, not {self.start_cwd}: its coverage "
                    "names may not match"
                )
        self.collected, self.collect_errors = merge_collect(workers)
        order = [item.nodeid for item in self.collected]
        self.results.close()
        merged = merge_results(workers, self._folded, order)
        self.results.results = merged
        self._attempt("results.jsonl", lambda: write_jsonl(out / RESULTS_FILE, (r.as_dict() for r in merged)))
        sources = [w.coverage_file for w in workers if w.coverage_file.is_file()]
        problems = merge_data_files(self.coverage.data_file, sources)
        for problem in problems:
            self._note_error(f"coverage merge: {problem}")
        self._timed("coverage load", self.coverage.load)
        self._timed("coverage json", self.coverage.write_json)
        self._timed("coverage paths", self._repository_paths)
        rank = {nodeid: index for index, nodeid in enumerate(order)}
        started = time.perf_counter()
        try:
            shapes = merge_shapes(((w.id, w.shapes_file) for w in workers), out / SHAPES_FILE, rank)
        except Exception as exc:
            self._record_error("shapes", exc)
        else:
            for problem in shapes.problems:
                self._note_error(f"shapes merge: {problem}")
            self.functions_traced = shapes.functions_traced
            self.functions_truncated = shapes.functions_truncated
        self.phase_seconds["shapes"] = time.perf_counter() - started

    def _note_error(self, message: str) -> None:
        """Add ``message`` to ``meta.errors`` as it is (once)."""
        if message not in self.errors:
            self.errors.append(message)

    def _meta(self, exit_code: int, counts: dict[str, int]) -> dict[str, Any]:
        """The serial ``meta.json`` with the workers' measurement facts merged in, plus ``xdist``."""
        meta = super()._meta(exit_code, counts)
        metas = self.worker_meta
        losses = [loss for m in metas for loss in m.get("tracer_losses") or ()]
        meta.update(
            tracer=_first(metas, "tracer"),
            tool_id=_first(metas, "tool_id"),
            tracer_lost_at=_first(metas, "tracer_lost_at"),
            tracer_losses=losses[:20],
            coverage_warnings=self.coverage.warnings,
            shapes_disabled_reason=_first(metas, "shapes_disabled_reason")
            if self.options.shapes
            else "disabled by --coverset-shapes off",
            functions_traced=self.functions_traced,
            functions_truncated=self.functions_truncated,
            tracer_errors=sum(int(m.get("tracer_errors") or 0) for m in metas),
            stop_requested=self.stop_requested or any(m.get("stop_requested") is True for m in metas),
        )
        timings = meta["timings"]
        timings["tracer_callback_s"] = round(sum(_timing(m, "tracer_callback_s") for m in metas), 6)
        timings["tracer_events"] = int(sum(_timing(m, "tracer_events") for m in metas))
        timings["context_switches"] = int(sum(_timing(m, "context_switches") for m in metas))
        timings["context_switch_s"] = round(sum(_timing(m, "context_switch_s") for m in metas), 6)
        meta["xdist"] = self._xdist_meta()
        return meta

    def _xdist_meta(self) -> dict[str, Any] | None:
        """``meta.xdist``: one entry per worker directory, named after it (a dead worker has no meta).

        ``finished`` is xdist's word when the node went down through the hook (the worker sent its
        ``coverset`` output), else whether the worker managed to write its ``meta.json``.
        """
        workers = []
        for output in self.workers:
            meta = output.meta or {}
            node = self._nodes.get(output.id, {})
            workers.append(
                {
                    "id": output.id,
                    "tests": sum((meta.get("counts") or {}).get(k, 0) for k in _OUTCOMES),
                    "exit_code": meta.get("exit_code"),
                    "session_wall_s": _timing(meta, "session_wall_s"),
                    "errors": len(meta.get("errors") or ()),
                    "finished": bool(node.get("finished", output.meta is not None)),
                }
            )
        return {
            "role": "controller",
            "run": self.token.run,
            "dist": getattr(self.config.option, "dist", None),
            "workers": workers,
            "nodes_down": {
                k: v for k, v in sorted(self._nodes.items()) if v.get("error") or not v.get("finished")
            },
            "merge_s": round(self.merge_seconds, 6),
        }

    def finalize(self, exit_code: int) -> None:
        """The serial finalisation over the merged files; worker directories go once it is clean."""
        try:
            super().finalize(exit_code)
        finally:
            if not self.errors and self._owns_workers_dir():
                shutil.rmtree(self.workers_dir, ignore_errors=True)

    def shutdown(self) -> None:
        """Withdraw the token, then the session's own teardown."""
        os.environ.pop(XDIST_ENV, None)
        super().shutdown()


_OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")


def _first(metas: Sequence[Mapping[str, Any]], key: str) -> Any:
    return next((m[key] for m in metas if m.get(key) is not None), None)


def _timing(meta: Mapping[str, Any], key: str) -> float:
    value = (meta.get("timings") or {}).get(key)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0.0
