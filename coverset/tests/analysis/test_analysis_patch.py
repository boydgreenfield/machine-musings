"""Patch coverage and changed-function rows."""

from __future__ import annotations

import textwrap

from coverset.analysis.functions import FunctionChange, index_functions
from coverset.analysis.patch import patch_report
from coverset.lang.base import IMPORT_CONTEXT, StaticFile

PATH = "src/ledger/billing.py"
SRC = textwrap.dedent(
    """\
    import functools


    @functools.cache
    def allocate(total):
        share = total // 2
        return share


    @functools.cache
    def untested(x):
        return x  # pragma: no cover


    def renamed_one(y):
        return y


    LIMIT = 4
    """
)
R = {r.qualname: r for r in index_functions(PATH, SRC)}
STATIC = StaticFile(
    PATH, 19, statements=(1, 4, 5, 6, 7, 10, 11, 12, 15, 16, 19), excluded=(12,), arcs_possible=()
)
# Import time ran the decorators, defs and LIMIT; tests ran allocate's body and renamed_one's.
IMPORTED = {1, 4, 5, 10, 11, 15, 19}
UNION = {PATH: IMPORTED | {6, 7, 16}}
TESTS = {(6, 7): ["tests/t.py::test_alloc", IMPORT_CONTEXT], (16, 16): ["tests/t.py::test_renamed"]}


def _covering(path: str, start: int, end: int) -> list[str]:
    return TESTS.get((start, end), [])


def test_patch_rules() -> None:
    changes = [
        FunctionChange(R["allocate"], R["allocate"], "modified"),
        FunctionChange(R["untested"], R["untested"], "modified"),
        FunctionChange(R["renamed_one"], R["renamed_one"], "renamed"),
        FunctionChange(R["<module>"], R["<module>"], "modified"),
        FunctionChange(None, R["allocate"], "deleted"),
    ]
    # Changed: allocate's decorator (4) and body (6); untested's decorator (10) and excluded line (12);
    # a blank line (8); LIMIT (19); a file without static facts.
    changed = {PATH: {4, 6, 8, 10, 12, 19}, "README.md": {1}}
    report = patch_report(changed, {PATH: STATIC}, UNION, changes, _covering)

    file = report.per_file[PATH]
    assert (file.covered, file.total) == (3, 4)  # 4, 6 and 19 covered; 10 is a decorator whose body never ran
    assert file.missing == (10,)
    assert set(report.per_file) == {PATH}
    assert report.patch_pct == 75.0

    rows = {row.change.region.qualname: row for row in report.function_rows}
    assert set(rows) == {"allocate", "untested", "renamed_one"}
    assert rows["allocate"].tests == ["tests/t.py::test_alloc"]  # <import> never counts as a test
    assert rows["allocate"].line_pct == 100.0
    assert rows["untested"].tests == [] and rows["untested"].line_pct is None  # only line excluded
    assert rows["renamed_one"].inherited and not rows["allocate"].inherited
    assert [r.change.region.qualname for r in report.zero_test_functions] == ["untested"]
    assert [(i.reason, (i.change.region or i.change.prev).qualname) for i in report.indirect] == [
        ("module-level change", "<module>"),
        ("deleted", "allocate"),
    ]


def test_decorator_only_change_is_not_covered_by_import() -> None:
    changes = [FunctionChange(R["allocate"], R["allocate"], "modified")]
    report = patch_report({PATH: {4}}, {PATH: STATIC}, {PATH: IMPORTED}, changes, _covering)
    assert (report.covered, report.total, report.patch_pct) == (0, 1, 0.0)
    report = patch_report({PATH: {4}}, {PATH: STATIC}, UNION, changes, _covering)  # the body ran
    assert report.patch_pct == 100.0


def test_no_changed_statements_gives_none() -> None:
    report = patch_report({PATH: {2, 3}}, {PATH: STATIC}, UNION, [], _covering)
    assert (report.patch_pct, report.total, report.per_file) == (None, 0, {})


def test_docstring_only_and_one_line_defs_need_a_test_call() -> None:
    path = "src/ledger/stubs.py"
    source = 'def stub():\n    """Doc."""\n\n\ndef one(x): return x\n\n\ndef called(x): return x\n'
    regions = {r.qualname: r for r in index_functions(path, source)}
    static = StaticFile(path, 8, statements=(1, 5, 8), excluded=(), arcs_possible=())
    # Import time executes all three def lines; only test_called calls a function (line 8).
    contexts = {
        (1, 1): [IMPORT_CONTEXT],
        (5, 5): [IMPORT_CONTEXT],
        (8, 8): [IMPORT_CONTEXT, "t.py::test_called"],
    }
    changes = [FunctionChange(r, r, "modified") for name, r in regions.items() if name != "<module>"]
    report = patch_report(
        {path: {1, 5, 8}},
        {path: static},
        {path: {1, 5, 8}},
        changes,
        lambda p, start, end: contexts.get((start, end), []),
    )
    assert report.per_file[path].missing == (1, 5)
    rows = {row.change.region.qualname: row for row in report.function_rows}
    assert (rows["stub"].tests, rows["stub"].line_pct) == ([], 0.0)
    assert (rows["one"].tests, rows["one"].line_pct) == ([], 0.0)
    assert (rows["called"].tests, rows["called"].line_pct) == (["t.py::test_called"], 100.0)
    assert [r.change.region.qualname for r in report.zero_test_functions] == ["stub", "one"]
