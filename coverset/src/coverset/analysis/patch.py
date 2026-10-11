"""Patch coverage and changed-function coverage of one run.

Rules:

* patch coverage = ``|changed ∩ statements ∩ executed| / |changed ∩ statements|`` with excluded
  lines removed; files without static facts (not measured, e.g. docs) are left out; ``executed``
  is the run's union over all tests including ``<import>``.
* a changed decorator or signature line (``def_line`` up to ``body_start - 1`` of a function
  region) counts as covered only when some body line of that function ran: import time executes
  every ``def`` line, which says nothing about whether the function was tested.
* when ``body_start == def_line`` (a one-line ``def`` or a docstring-only body, see
  :mod:`coverset.analysis.functions`) the ``def`` line is both signature and body; it counts as
  covered, in the patch and in the row's line percentage, only when a test (not ``<import>``)
  executed it, i.e. called the function.
* one row per changed function or method (not deleted); ``trivial``, ``renamed`` and ``moved``
  rows are marked inherited. A row's tests are the tests whose lines touch ``[body_start,
  end_line]``; its line percentage is over the body's statements.
* functions with no covering test are called out unless their row is inherited.
* deleted functions and class- or module-level changes are listed as indirect changes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from coverset.analysis.diff import is_behaviour_preserving
from coverset.analysis.functions import FunctionChange, FunctionRegion
from coverset.lang.base import IMPORT_CONTEXT, StaticFile

__all__ = ["FilePatch", "FunctionRow", "IndirectChange", "PatchReport", "patch_report"]

type Covering = Callable[[str, int, int], list[str]]


@dataclass(frozen=True, slots=True)
class FilePatch:
    """Patch coverage of one file."""

    path: str
    covered: int
    total: int
    missing: tuple[int, ...]
    """Changed statement lines no test executed (or signature lines whose body never ran)."""

    @property
    def pct(self) -> float | None:
        """Covered percentage, or None when the file has no changed statements."""
        return _pct(self.covered, self.total)


@dataclass(frozen=True, slots=True)
class FunctionRow:
    """One changed function: its covering tests and body line coverage."""

    change: FunctionChange
    tests: list[str]
    line_pct: float | None
    inherited: bool


@dataclass(frozen=True, slots=True)
class IndirectChange:
    """A change that is not a function row: a deleted function or a class/module-level edit."""

    change: FunctionChange
    reason: str


@dataclass(frozen=True, slots=True)
class PatchReport:
    """Patch, per-file and per-function coverage of a diff in one run."""

    patch_pct: float | None
    covered: int
    total: int
    per_file: dict[str, FilePatch] = field(default_factory=dict)
    function_rows: list[FunctionRow] = field(default_factory=list)
    zero_test_functions: list[FunctionRow] = field(default_factory=list)
    indirect: list[IndirectChange] = field(default_factory=list)


def patch_report(
    changed_lines: Mapping[str, set[int]],
    statics: Mapping[str, StaticFile],
    union_lines: Mapping[str, set[int]],
    changes: Sequence[FunctionChange],
    covering: Covering,
) -> PatchReport:
    """Apply the module's rules to the head-side ``changed_lines`` of a diff.

    ``covering(path, start, end)`` returns the tests whose lines touch ``[start, end]`` of ``path``
    in this run (``<import>`` may appear and is ignored).
    """
    head_funcs: dict[str, list[FunctionRegion]] = {}
    for change in changes:
        region = change.region
        if region is not None and change.kind != "deleted" and region.kind in ("function", "method"):
            head_funcs.setdefault(region.path, []).append(region)
    calls: dict[str, list[str]] = {}

    def callers(region: FunctionRegion) -> list[str]:
        if region.func_key not in calls:
            found = covering(region.path, region.body_start, region.end_line)
            calls[region.func_key] = sorted(t for t in found if t != IMPORT_CONTEXT)
        return calls[region.func_key]

    per_file: dict[str, FilePatch] = {}
    for path in sorted(changed_lines):
        static = statics.get(path)
        if static is None:
            continue
        statements = set(static.statements) - set(static.excluded)
        relevant = set(changed_lines[path]) & statements
        if not relevant:
            continue
        executed = union_lines.get(path, set())
        gates = _signature_gates(head_funcs.get(path, []), statements, executed, callers)
        covered = {line for line in relevant if line in executed and gates.get(line, True)}
        per_file[path] = FilePatch(path, len(covered), len(relevant), tuple(sorted(relevant - covered)))

    rows: list[FunctionRow] = []
    indirect: list[IndirectChange] = []
    for change in changes:
        region = change.region
        if change.kind == "deleted" or region is None:
            indirect.append(IndirectChange(change, "deleted"))
        elif region.kind in ("class", "module"):
            indirect.append(IndirectChange(change, f"{region.kind}-level change"))
        else:
            tests = callers(region)
            line_pct = _body_line_pct(
                region, statics.get(region.path), union_lines.get(region.path, set()), called=bool(tests)
            )
            rows.append(FunctionRow(change, tests, line_pct, is_behaviour_preserving(change)))

    covered_total = sum(f.covered for f in per_file.values())
    total = sum(f.total for f in per_file.values())
    return PatchReport(
        patch_pct=_pct(covered_total, total),
        covered=covered_total,
        total=total,
        per_file=per_file,
        function_rows=rows,
        zero_test_functions=[r for r in rows if not r.tests and not r.inherited],
        indirect=indirect,
    )


def _signature_gates(
    regions: Sequence[FunctionRegion],
    statements: set[int],
    executed: set[int],
    callers: Callable[[FunctionRegion], list[str]],
) -> dict[int, bool]:
    """Map each decorator/signature line to whether its function's body ran (innermost wins)."""
    gates: dict[int, bool] = {}
    for region in sorted(regions, key=lambda r: r.def_line):
        if region.body_start <= region.def_line:
            gates[region.def_line] = bool(callers(region))
            continue
        body = {n for n in statements if region.body_start <= n <= region.end_line}
        body_ran = bool(body & executed)
        for line in range(region.def_line, region.body_start):
            gates[line] = body_ran
    return gates


def _body_line_pct(
    region: FunctionRegion, static: StaticFile | None, executed: set[int], *, called: bool
) -> float | None:
    if static is None:
        return None
    statements = set(static.statements) - set(static.excluded)
    body = {n for n in statements if region.body_start <= n <= region.end_line}
    ran = body & executed
    if region.body_start <= region.def_line and not called:
        ran.discard(region.def_line)  # import time ran the def line; no test called the function
    return _pct(len(ran), len(body))


def _pct(covered: int, total: int) -> float | None:
    return round(100.0 * covered / total, 1) if total else None
