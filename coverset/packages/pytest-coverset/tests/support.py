"""Helpers shared by the pytest-coverset tests: event parsing and bundle readers."""

from __future__ import annotations

import gzip
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

FIXTURE_PROJECT = Path(__file__).parent / "fixture_project"
NONCE = "0123456789abcdef0123456789abcdef"
PREFIX = f"\x1ecoverset {NONCE} "


def events(pytester: pytest.Pytester) -> list[dict[str, Any]]:
    """Every coverset event in the stdout of the last subprocess run in ``pytester``.

    The raw stdout file is split on ``\\n`` only: ``str.splitlines()`` (and so ``RunResult.outlines``)
    treats the ``\\x1e`` record separator that starts every event as a line boundary.
    """
    raw = (pytester.path / "stdout").read_text(encoding="utf-8")
    return [json.loads(line[len(PREFIX) :]) for line in raw.split("\n") if line.startswith(PREFIX)]


@dataclass
class Shapes:
    """A decoded ``shapes.jsonl.gz``."""

    header: dict[str, Any]
    obs: list[dict[str, Any]]
    sampling: list[dict[str, Any]]

    def func_index(self, qualname: str) -> int:
        """Index of the function with ``qualname`` in the header."""
        matches = [i for i, f in enumerate(self.header["funcs"]) if f[1] == qualname]
        assert len(matches) == 1, (qualname, self.header["funcs"])
        return matches[0]

    def keys(self, qualname: str, param: str) -> set[str]:
        """Shape keys observed for ``param`` of ``qualname``."""
        f = self.func_index(qualname)
        return {self.header["shapes"][row["s"]] for row in self.obs if row["f"] == f and row["p"] == param}

    def rows(self, qualname: str, param: str | None = None) -> list[dict[str, Any]]:
        """Observation rows for ``qualname`` (optionally one parameter)."""
        f = self.func_index(qualname)
        return [row for row in self.obs if row["f"] == f and (param is None or row["p"] == param)]

    def params(self, qualname: str) -> set[str]:
        """Parameter labels recorded for ``qualname``."""
        return {row["p"] for row in self.rows(qualname)}

    def sampling_of(self, qualname: str) -> dict[str, Any]:
        """The ``func_sampling`` row of ``qualname``."""
        f = self.func_index(qualname)
        return next(row for row in self.sampling if row["f"] == f)


def read_shapes(out: Path) -> Shapes:
    """Decode ``out/shapes.jsonl.gz``."""
    with gzip.open(out / "shapes.jsonl.gz", "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle]
    header, rows = lines[0], lines[1:]
    assert header["kind"] == "header"
    return Shapes(
        header=header,
        obs=[r for r in rows if r["kind"] == "obs"],
        sampling=[r for r in rows if r["kind"] == "func_sampling"],
    )


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSON-lines file."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def coverage_rows(sqlite_path: Path) -> set[tuple[str, str, int, int]]:
    """All (context, file, from_line, to_line) arc rows of a coverage SQLite file."""
    with closing(sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)) as con:
        rows = con.execute(
            "select context.context, file.path, arc.fromno, arc.tono from arc "
            "join file on file.id = arc.file_id join context on context.id = arc.context_id"
        ).fetchall()
    return set(rows)


def contexts(sqlite_path: Path) -> list[str]:
    """Every context recorded in a coverage SQLite file."""
    with closing(sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)) as con:
        return [row[0] for row in con.execute("select context from context order by id")]
