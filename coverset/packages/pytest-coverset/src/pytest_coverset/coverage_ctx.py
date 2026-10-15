"""Per-test coverage with coverage.py's ctrace core and one dynamic context per test.

coverage.py's sysmon core does not support dynamic contexts, so ``run:core`` is forced to ``ctrace``.
The context of a test is exactly its nodeid (``run:context`` is cleared so no static context is
prefixed); code that runs before the first test (imports, collection) stays in the ``''`` context.
Every ``switch_context`` flushes collected data into the SQLite file, so a test's data is on disk as
soon as the next test starts; a failed test additionally flushes immediately.

coverage.py's expected warnings (a ``--coverset-source`` package the shard never imports, and so
on) are disabled by slug. Every other :class:`CoverageWarning` raised around a coverage call is
captured, never raised, so a target repo running with ``-W error`` or ``filterwarnings = error``
cannot turn it into an exception inside a pytest hook; the captured messages go to ``meta.json``.

Tracer loss: a test that calls ``sys.settrace(None)`` (or installs its own trace function and does
not restore coverage's) silently stops measurement for the rest of the shard. Before every context
switch :meth:`CoverageContext.switch` compares ``sys.gettrace()`` with the collector's tracer. On a
mismatch it records the context that was running (``tracer_lost_at``) and re-installs the tracer
through coverage.py's public ``stop()``/``start()`` when this Coverage object is the active one; when
another Coverage object sits on top of it (pytest-cov started without ``--no-cov``) it records only.

Repository-relative names: coverage.py (``relative_files``) names a file under the working
directory relative to it and any other file absolute, so a run from a subdirectory would name
``src/pkg/x.py`` as ``../src/pkg/x.py``'s absolute path or as ``x.py``. After ``coverage.json`` is
written, :meth:`CoverageContext.remap_files` renames every file of both data files through
:func:`pytest_coverset.paths.coverage_name`: ``coverage.json``'s keys are rewritten, and
``coverage.sqlite`` is rebuilt with coverage.py's public ``CoverageData.update(..., map_path=)`` (the
same call ``coverage combine`` uses for ``[paths]``). Both are written under temporary names and
renamed into place; nothing is rebuilt when no name changes (a run from the git root).

Write cost: coverage.py opens, configures (two pragmas, a ``REGEXP`` function) and closes its SQLite
connection around every write, so each ``switch_context`` cost about 177 µs, most of it connecting
(2,000 switches of a one-function module on an M-series Mac; 25 µs once the connection stays
open). :func:`held_connection` keeps one connection open across writes by entering coverage.py's
own ``SqliteDb`` context once (its nesting counter then skips the connect, commit and close of every
inner write). :class:`CoverageContext` holds it from the first context switch that wrote data and
commits at most every :data:`COMMIT_INTERVAL_S` (and after a failed test). In a serial run nothing
reads ``coverage.sqlite`` before this process packs the bundle, so a SIGKILL that loses up to a second
of per-test lines loses nothing extra: the in-memory shapes and the bundle are gone too. An xdist
worker is different: when it dies, the controller still merges its data file next to results that
say its earlier tests passed, so a worker commits at every switch (``commit_interval_s = 0``; with
coverage.py's ``journal_mode=off`` and ``synchronous=off`` a commit is a few microseconds and no
fsync) and only the test that was running when it died loses its lines.
The hold relies on two private names (``CoverageData._connect`` and ``SqliteDb.nest``); when they
are missing (another coverage.py version) nothing is held and every write behaves as before.
:func:`merge_data_files` combines several data files (pytest-xdist workers) with the public
``CoverageData.update``, which keeps every context.
"""

from __future__ import annotations

import json
import os
import sys
import time
import warnings
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import coverage
from coverage.exceptions import CoverageWarning, NoDataError

DATA_FILE = "coverage.sqlite"
JSON_FILE = "coverage.json"
JSON_FORMAT_VERSION = 3
SILENCED_WARNINGS = (
    "module-not-imported",
    "module-not-measured",
    "no-data-collected",
    "module-not-python",
    "include-ignored",
)
"""coverage.py warning slugs that are expected when a shard imports only part of the sources."""
IMPORT_CONTEXT = "<import>"
"""How ``tracer_lost_at`` names the ``''`` context (imports and collection, before the first test)."""
MAX_WARNINGS = 50
MAX_LOSSES = 20
COMMIT_INTERVAL_S = 1.0
"""How long per-test data may sit in an open SQLite transaction before it is committed."""


@contextmanager
def held_connection(data: coverage.CoverageData) -> Iterator[bool]:
    """Keep ``data``'s SQLite connection (this thread's) open for the block; yields whether it is held.

    Every coverage.py read or write inside the block reuses the connection instead of connecting,
    configuring and closing it again, and writes stay in one transaction that is committed when the
    block ends. Without the private API this needs, the block is a no-op (yields False).
    """
    db = _enter_db(data)
    try:
        yield db is not None
    finally:
        _exit_db(db)


def _enter_db(data: coverage.CoverageData) -> Any:
    """Enter coverage.py's ``SqliteDb`` for this thread once (its nest counter then skips reconnects)."""
    connect = getattr(data, "_connect", None)
    if connect is None:
        return None
    try:
        db = connect()
        if not hasattr(db, "nest") or not hasattr(db, "con"):
            return None
        db.__enter__()
    except Exception:
        return None
    return db


def _exit_db(db: Any) -> None:
    """Leave a ``SqliteDb`` entered by :func:`_enter_db`: commit and close when it is the outermost."""
    if db is None:
        return
    with suppress(Exception):
        db.__exit__(None, None, None)


def merge_data_files(target: Path, sources: Iterable[Path]) -> list[str]:
    """Combine coverage data files into ``target`` (replaced) with ``CoverageData.update``.

    Contexts are kept (a test's lines stay under its node id) and file names are not mapped, so the
    sources must name files the same way (workers that ran in one working directory). A source
    that cannot be read is skipped; the messages are returned.
    """
    problems: list[str] = []
    target.unlink(missing_ok=True)
    combined = coverage.CoverageData(basename=str(target), suffix=False)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", CoverageWarning)
            for source in sources:
                part = coverage.CoverageData(basename=str(source), suffix=False)
                try:
                    part.read()
                    combined.update(part)  # one transaction per source, inside coverage.py
                except Exception as exc:
                    problems.append(f"{source}: {type(exc).__name__}: {exc}")
                finally:
                    part.close(force=True)
    finally:
        combined.close(force=True)
    return problems


class ForeignTracerError(RuntimeError):
    """Raised when another tool already installed a ``sys.settrace`` function."""


class CoverageContext:
    """Owns the session's :class:`coverage.Coverage` object."""

    def __init__(self, out_dir: Path, sources: Sequence[str]) -> None:
        self.out_dir = out_dir
        self.data_file = out_dir / DATA_FILE
        self.json_file = out_dir / JSON_FILE
        self.cov = coverage.Coverage(
            data_file=str(self.data_file),
            data_suffix=False,
            branch=True,
            source=list(sources) or None,
            config_file=True,
        )
        self.cov.set_option("run:core", "ctrace")
        self.cov.set_option("run:relative_files", True)
        self.cov.set_option("run:dynamic_context", None)
        self.cov.set_option("run:context", None)
        self.cov.set_option("run:parallel", False)
        self.cov.set_option("run:disable_warnings", list(SILENCED_WARNINGS))
        self.tracer: str | None = None
        self.context: str = ""
        self._running = False
        self.warnings: list[str] = []
        """Captured :class:`CoverageWarning` messages (at most :data:`MAX_WARNINGS`)."""
        self.tracer_lost_at: str | None = None
        """The context during which coverage's trace function was first found missing."""
        self.tracer_losses: list[dict[str, Any]] = []
        """The first :data:`MAX_LOSSES` detected losses: ``{"context", "found", "reinstalled"}``."""
        self.tracer_lost_count = 0
        """Context switches at which the tracer was found missing (one per affected test)."""
        self.switches = 0
        self.switch_seconds = 0.0
        """Time spent in :meth:`switch` (coverage's flush of the previous test included)."""
        self._held: Any = None
        self._last_commit = 0.0
        self.commit_interval_s = COMMIT_INTERVAL_S
        """Longest wait between commits of the held connection; 0 commits at every switch."""

    @contextmanager
    def _quiet(self) -> Iterator[None]:
        """Capture :class:`CoverageWarning` regardless of the active warning filters (never raise it).

        Any other warning raised meanwhile is re-issued once the coverage call is over.
        """
        caught: list[warnings.WarningMessage] = []
        try:
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", CoverageWarning)
                yield
        finally:
            for item in caught:
                if issubclass(item.category, CoverageWarning):
                    self._note_warning(str(item.message))
                    continue
                with suppress(Exception):
                    warnings.warn_explicit(item.message, item.category, item.filename, item.lineno)

    def _note_warning(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(message)

    def start(self) -> None:
        """Erase stale data and start measuring; raise :class:`ForeignTracerError` if a tracer is set."""
        existing = sys.gettrace()
        if existing is not None:
            raise ForeignTracerError(f"another tracer is already installed: {existing!r}")
        with self._quiet():
            self.cov.erase()
            self.cov.start()
        self._running = True
        self.tracer = self._tracer_name()

    @property
    def running(self) -> bool:
        """True between :meth:`start` and :meth:`stop`."""
        return self._running

    def switch(self, nodeid: str) -> None:
        """Attribute everything measured from now on to ``nodeid`` (after checking the tracer is ours)."""
        if self._running and nodeid != self.context:
            started = time.perf_counter()
            self.check_tracer()
            with self._quiet():
                self.cov.switch_context(nodeid)
            self.context = nodeid
            self._written()
            self.switches += 1
            self.switch_seconds += time.perf_counter() - started

    def _written(self, *, commit: bool = False) -> None:
        """After a flush: hold the connection once data exists, commit every ``commit_interval_s``."""
        now = time.monotonic()
        if self._held is None:
            data = getattr(self.cov, "_data", None)
            # Not before coverage.py's first write: that write erases the file (`_start_using`).
            if data is not None and getattr(data, "_have_used", False):
                self._held = _enter_db(data) or False
                self._last_commit = now
            return
        if self._held and (commit or now - self._last_commit >= self.commit_interval_s):
            with suppress(Exception):
                self._held.con.commit()
            self._last_commit = now

    def _release(self) -> None:
        """Commit and close the held connection (before coverage.py saves or anything reads the file)."""
        held, self._held = self._held, None
        if held:
            _exit_db(held)

    def _our_tracers(self) -> list[object]:
        collector = getattr(self.cov, "_collector", None)
        return list(getattr(collector, "tracers", None) or ())

    def tracer_installed(self) -> bool:
        """True when this thread's trace function is one of this Coverage object's tracers."""
        current = sys.gettrace()
        return any(current is tracer for tracer in self._our_tracers())

    def check_tracer(self, *, repair: bool = True) -> bool:
        """Detect (and with ``repair``, when possible, fix) a lost trace function; True when intact.

        A loss is recorded against the running context: ``tracer_lost_at`` keeps the first one.
        """
        if not self._running or self.tracer_installed():
            return True
        current = sys.gettrace()
        found = "none" if current is None else _describe(current)
        context = self.context or IMPORT_CONTEXT
        reinstalled = self._reinstall() if repair else False
        if self.tracer_lost_at is None:
            self.tracer_lost_at = context
        self.tracer_lost_count += 1
        if len(self.tracer_losses) < MAX_LOSSES:
            self.tracer_losses.append({"context": context, "found": found, "reinstalled": reinstalled})
        return False

    def _on_top(self) -> bool:
        """True when this Coverage's collector is the active one (no other Coverage started after it)."""
        collector = getattr(self.cov, "_collector", None)
        stack = getattr(type(collector), "_collectors", None) if collector is not None else None
        return bool(stack) and stack[-1] is collector

    def _reinstall(self) -> bool:
        """Re-install coverage's tracer with ``stop()``/``start()`` when this Coverage is the active one."""
        if not self._on_top():
            return False  # another Coverage object (pytest-cov) is on top: stop() would assert
        try:
            with self._quiet():
                self.cov.stop()
                self.cov.start()
        except Exception as exc:
            self._note_warning(f"re-installing the coverage tracer failed: {type(exc).__name__}: {exc}")
            return False
        return self.tracer_installed()

    def flush(self) -> None:
        """Write the current context's data to the SQLite file now (and commit it)."""
        if self._running:
            with self._quiet():
                self.cov.get_data()
            self._written(commit=True)

    def stop(self) -> None:
        """Stop measuring and save the data file (saved even when stopping fails). Idempotent."""
        if not self._running:
            return
        self.check_tracer(repair=False)
        self._running = False
        self._release()
        with self._quiet():
            try:
                if self._on_top():
                    self.cov.stop()
            finally:
                self.cov.save()

    def load(self) -> None:
        """Read an existing data file (merged from xdist workers) instead of measuring.

        Without this a report would start a fresh, never-read data object, and coverage.py erases
        the file on such an object's first use.
        """
        if self.data_file.is_file():
            with self._quiet():
                self.cov.load()

    def write_json(self) -> None:
        """Write ``coverage.json`` (format 3); an empty report when nothing was measured."""
        try:
            with self._quiet():
                self.cov.json_report(outfile=str(self.json_file), pretty_print=False, ignore_errors=True)
        except NoDataError:
            empty = {
                "meta": {
                    "format": JSON_FORMAT_VERSION,
                    "version": coverage.__version__,
                    "branch_coverage": True,
                },
                "files": {},
                "totals": {},
            }
            self.json_file.write_text(json.dumps(empty), encoding="utf-8")

    def remap_files(self, mapper: Callable[[str], str]) -> int:
        """Rename every file of ``coverage.json`` and ``coverage.sqlite`` through ``mapper``.

        Call after :meth:`stop` and :meth:`write_json`. Returns how many files ``coverage.json``
        lists with a relative (repository) name afterwards. Two names that map to one path are
        merged in ``coverage.sqlite`` (coverage.py unions them) and the first is kept in
        ``coverage.json``. Raises when either file cannot be rewritten; then neither was replaced,
        unless the failure was renaming ``coverage.json`` after ``coverage.sqlite`` was replaced.
        """
        try:
            report = json.loads(self.json_file.read_text(encoding="utf-8"))
        except FileNotFoundError:  # writing it failed (recorded): the data file is still remapped
            report = None
        files = report.get("files") if isinstance(report, dict) else None
        renamed: dict[str, Any] = {}
        for name, entry in (files or {}).items():
            renamed.setdefault(mapper(name), entry)
        json_changed = isinstance(report, dict) and isinstance(files, dict) and list(renamed) != list(files)
        sqlite_tmp: Path | None = None
        json_tmp = self.json_file.with_name(self.json_file.name + ".tmp")
        try:
            if self.data_file.is_file():
                sqlite_tmp = self._remapped_data(mapper)
            if json_changed:
                assert isinstance(report, dict)
                report["files"] = renamed
                json_tmp.write_text(json.dumps(report), encoding="utf-8")
            if sqlite_tmp is not None:
                os.replace(sqlite_tmp, self.data_file)
                sqlite_tmp = None
            if json_changed:
                os.replace(json_tmp, self.json_file)
        finally:
            if sqlite_tmp is not None:
                sqlite_tmp.unlink(missing_ok=True)
            json_tmp.unlink(missing_ok=True)
        return sum(1 for name in renamed if not os.path.isabs(name))

    def _remapped_data(self, mapper: Callable[[str], str]) -> Path | None:
        """``coverage.sqlite`` rebuilt with mapped names under a temporary name; None when unchanged."""
        with self._quiet():
            original = coverage.CoverageData(basename=str(self.data_file), suffix=False)
            original.read()
            try:
                mapping = {name: mapper(name) for name in original.measured_files()}
                if all(name == mapped for name, mapped in mapping.items()):
                    return None
                tmp = self.data_file.with_name(self.data_file.name + ".remap")
                tmp.unlink(missing_ok=True)
                rebuilt = coverage.CoverageData(basename=str(tmp), suffix=False)
                try:
                    rebuilt.update(original, map_path=lambda name: mapping.get(name, name))
                    if rebuilt.has_arcs() != original.has_arcs():
                        raise RuntimeError("the rebuilt coverage data lost its branch (arc) flag")
                except BaseException:
                    rebuilt.close(force=True)
                    tmp.unlink(missing_ok=True)
                    raise
                rebuilt.close(force=True)
            finally:
                original.close(force=True)
        return tmp

    def _tracer_name(self) -> str | None:
        try:
            collector = self.cov._collector
            return collector.tracer_name() if collector is not None else None
        except Exception:
            return None


def _describe(tracer: object) -> str:
    """``module.QualName`` of a trace function's type (never its ``repr``: that could be user code)."""
    cls = type(tracer)
    module = type.__dict__["__module__"].__get__(cls)
    qualname = type.__dict__["__qualname__"].__get__(cls)
    return f"{module}.{qualname}" if type(module) is str else str(qualname)
