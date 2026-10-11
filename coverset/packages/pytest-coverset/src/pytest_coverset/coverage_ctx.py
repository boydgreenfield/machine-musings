"""Per-test coverage with coverage.py's ctrace core and one dynamic context per test.

coverage.py's sysmon core does not support dynamic contexts, so ``run:core`` is forced to ``ctrace``.
The context of a test is exactly its nodeid (``run:context`` is cleared so no static context is
prefixed); code that runs before the first test (imports, collection) stays in the ``''`` context.
Every ``switch_context`` flushes collected data into the SQLite file, so a test's data is on disk as
soon as the next test starts; a failed test additionally flushes immediately.

coverage.py's own warnings (a ``--coverset-source`` package the shard never imports, and so on) are
disabled and additionally filtered around every call, so a target repo running with ``-W error`` or
``filterwarnings = error`` cannot turn them into exceptions inside pytest hooks.
"""

from __future__ import annotations

import json
import sys
import warnings
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

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


@contextmanager
def _quiet() -> Iterator[None]:
    """Ignore :class:`CoverageWarning` regardless of the active warning filters."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", CoverageWarning)
        yield


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

    def start(self) -> None:
        """Erase stale data and start measuring; raise :class:`ForeignTracerError` if a tracer is set."""
        existing = sys.gettrace()
        if existing is not None:
            raise ForeignTracerError(f"another tracer is already installed: {existing!r}")
        with _quiet():
            self.cov.erase()
            self.cov.start()
        self._running = True
        self.tracer = self._tracer_name()

    @property
    def running(self) -> bool:
        """True between :meth:`start` and :meth:`stop`."""
        return self._running

    def switch(self, nodeid: str) -> None:
        """Attribute everything measured from now on to ``nodeid``."""
        if self._running and nodeid != self.context:
            with _quiet():
                self.cov.switch_context(nodeid)
            self.context = nodeid

    def flush(self) -> None:
        """Write the current context's data to the SQLite file now."""
        if self._running:
            with _quiet():
                self.cov.get_data()

    def stop(self) -> None:
        """Stop measuring and save the data file. Idempotent."""
        if not self._running:
            return
        self._running = False
        with _quiet():
            self.cov.stop()
            self.cov.save()

    def write_json(self) -> None:
        """Write ``coverage.json`` (format 3); an empty report when nothing was measured."""
        try:
            with _quiet():
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
