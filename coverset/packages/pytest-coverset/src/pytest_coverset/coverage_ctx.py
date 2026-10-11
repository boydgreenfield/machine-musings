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
"""

from __future__ import annotations

import json
import sys
import warnings
from collections.abc import Iterator, Sequence
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
            self.check_tracer()
            with self._quiet():
                self.cov.switch_context(nodeid)
            self.context = nodeid

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
        """Write the current context's data to the SQLite file now."""
        if self._running:
            with self._quiet():
                self.cov.get_data()

    def stop(self) -> None:
        """Stop measuring and save the data file (saved even when stopping fails). Idempotent."""
        if not self._running:
            return
        self.check_tracer(repair=False)
        self._running = False
        with self._quiet():
            try:
                if self._on_top():
                    self.cov.stop()
            finally:
                self.cov.save()

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
