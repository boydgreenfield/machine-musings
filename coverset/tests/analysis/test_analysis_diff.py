"""Diff parsing on real git repositories and function-change classification."""

from __future__ import annotations

import textwrap
import time
from pathlib import Path

import pytest
from analysis_helpers import commit_tree, git, tree_at

from coverset.analysis import diff as diff_module
from coverset.analysis.diff import (
    FileDiff,
    GitDiffError,
    changed_functions,
    parse_diff,
    run_git_diff,
    validate_ref,
)
from coverset.analysis.diff import _unquote_git_path as _unquote_git_path
from coverset.lang.python import PythonAdapter

BILLING = textwrap.dedent(
    """\
    RATE = 2


    def allocate(total, weights):
        share = total / sum(weights)
        return [share * w for w in weights]


    def refund(amount):
        if amount < 0:
            raise ValueError("negative")
        return -amount


    def helper(values):
        total = 0
        for value in values:
            total += value * RATE
        return total
    """
)


def _changes(repo: Path, base: str, head: str):
    diffs = parse_diff(run_git_diff(repo, base, head))
    base_tree = tree_at(repo, base, {d.base_path for d in diffs.values()})
    head_tree = tree_at(repo, head, set(diffs))
    return diffs, changed_functions(PythonAdapter(), base_tree, head_tree, diffs)


def _kinds(changes) -> list[tuple[str, str, str | None]]:
    return [
        (c.kind, c.region.qualname if c.region else "-", c.prev.qualname if c.prev else None) for c in changes
    ]


def test_parse_diff_added_removed_renamed_deleted(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"pkg/billing.py": BILLING, "pkg/old.py": "X = 1\n"}, "base")
    edited = BILLING.replace("return -amount", "return 0 - amount").replace("RATE = 2", "RATE = 3")
    commit_tree(git_repo, {"pkg/billing.py": None, "pkg/old.py": None}, "move")
    head = commit_tree(git_repo, {"pkg/money.py": edited, "pkg/new.py": "Y = 2\n"}, "edit")
    diffs = parse_diff(run_git_diff(git_repo, base, head))
    assert set(diffs) == {"pkg/money.py", "pkg/new.py", "pkg/old.py"}
    money = diffs["pkg/money.py"]
    assert (money.status, money.previous_path) == ("renamed", "pkg/billing.py")
    assert money.added == {1, 12} and money.removed == {1, 12}
    assert diffs["pkg/new.py"].status == "added" and diffs["pkg/new.py"].added == {1}
    assert diffs["pkg/old.py"].status == "deleted" and diffs["pkg/old.py"].removed == {1}


def test_pure_rename_has_no_line_changes(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"a.py": BILLING}, "base")
    commit_tree(git_repo, {"a.py": None}, "rm")
    head = commit_tree(git_repo, {"b.py": BILLING}, "add")
    diffs, changes = _changes(git_repo, base, head)
    assert diffs["b.py"] == FileDiff("b.py", "renamed", frozenset(), frozenset(), "a.py")
    assert {c.kind for c in changes} == {"moved"}
    assert {c.region.qualname for c in changes} == {"<module>", "allocate", "refund", "helper"}


def test_copy_header_is_parsed() -> None:
    text = (
        "diff --git a/x.py b/y.py\nsimilarity index 90%\ncopy from x.py\ncopy to y.py\n"
        "index 1111111..2222222 100644\n--- a/x.py\n+++ b/y.py\n@@ -3 +3 @@\n-a = 1\n+a = 2\n"
    )
    assert parse_diff(text) == {"y.py": FileDiff("y.py", "copied", frozenset({3}), frozenset({3}), "x.py")}


def test_trivial_and_modified(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING}, "base")
    edited = BILLING.replace("    return -amount", "    return -amount  # credit").replace(
        "    share = total / sum(weights)", "    share = total // sum(weights)"
    )
    head = commit_tree(git_repo, {"m.py": edited}, "edit")
    _, changes = _changes(git_repo, base, head)
    assert _kinds(changes) == [("modified", "allocate", "allocate"), ("trivial", "refund", "refund")]


def test_module_level_change_yields_module_region(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING}, "base")
    head = commit_tree(git_repo, {"m.py": BILLING.replace("RATE = 2", "RATE = 5")}, "edit")
    _, changes = _changes(git_repo, base, head)
    assert _kinds(changes) == [("modified", "<module>", "<module>")]
    assert changes[0].region.kind == "module"


def test_added_and_deleted_functions(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING}, "base")
    without_helper = BILLING.split("\n\ndef helper")[0] + "\n"
    head = commit_tree(git_repo, {"m.py": without_helper + "\n\ndef brand_new():\n    return 42\n"}, "edit")
    _, changes = _changes(git_repo, base, head)
    assert ("added", "brand_new", None) in _kinds(changes)
    assert ("deleted", "-", "helper") in _kinds(changes)


def test_renamed_within_file(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING}, "base")
    head = commit_tree(git_repo, {"m.py": BILLING.replace("def helper(", "def scaled_total(")}, "rename")
    _, changes = _changes(git_repo, base, head)
    assert _kinds(changes) == [("renamed", "scaled_total", "helper")]


def test_moved_across_files(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING, "util.py": "PI = 3\n"}, "base")
    helper = "\n\ndef helper(values):" + BILLING.split("def helper(values):")[1]
    without_helper = BILLING.split("\n\ndef helper")[0] + "\n"
    head = commit_tree(git_repo, {"m.py": without_helper, "util.py": "PI = 3\n" + helper}, "move")
    _, changes = _changes(git_repo, base, head)
    moved = [c for c in changes if c.kind == "moved"]
    assert len(moved) == 1
    assert (moved[0].prev.path, moved[0].region.path) == ("m.py", "util.py")
    assert moved[0].region.qualname == "helper"
    assert not any(c.kind in ("added", "deleted") for c in changes)


def test_renamed_and_edited_pairs_by_similarity(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING}, "base")
    edited = BILLING.replace("def helper(values):", "def weighted_sum(values):").replace(
        "total += value * RATE", "total += value * RATE + 1"
    )
    head = commit_tree(git_repo, {"m.py": edited}, "rename+edit")
    _, changes = _changes(git_repo, base, head)
    assert _kinds(changes) == [("modified", "weighted_sum", "helper")]


def test_renamed_with_different_arity_is_not_paired(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING}, "base")
    edited = BILLING.replace("def helper(values):", "def weighted_sum(values, scale):").replace(
        "total += value * RATE", "total += value * RATE * scale"
    )
    head = commit_tree(git_repo, {"m.py": edited}, "rename+edit")
    _, changes = _changes(git_repo, base, head)
    assert sorted(c.kind for c in changes) == ["added", "deleted"]


def test_adapter_changed_functions_single_file() -> None:
    old = "def f(x):\n    return x\n"
    new = "def f(x):\n    return x + 1\n"
    changes = PythonAdapter().changed_functions("m.py", old, new, {2}, {2})
    assert [(c.kind, c.region.qualname) for c in changes] == [("modified", "f")]
    added = PythonAdapter().changed_functions("m.py", None, new, {1, 2}, set())
    assert {(c.kind, c.region.qualname) for c in added} == {("added", "f"), ("added", "<module>")}


@pytest.mark.parametrize("ref", ["--output=/tmp/x", "-p", "a..b", "x\0y", "", "foo bar", "HEAD~1"])
def test_bad_refs_are_rejected(ref: str) -> None:
    with pytest.raises(GitDiffError):
        validate_ref(ref)


@pytest.mark.parametrize("ref", ["main", "feature/x", "a" * 40, "HEAD"])
def test_good_refs_pass(ref: str) -> None:
    assert validate_ref(ref) == ref


def test_unknown_ref_raises(git_repo: Path) -> None:
    commit_tree(git_repo, {"a.py": "x = 1\n"}, "base")
    with pytest.raises(GitDiffError):
        run_git_diff(git_repo, "main", "nonexistent")


def test_renamed_with_new_signature_is_modified(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING}, "base")
    edited = BILLING.replace("def helper(values):", "def scaled_total(values=()):")
    head = commit_tree(git_repo, {"m.py": edited}, "rename+default")
    _, changes = _changes(git_repo, base, head)
    assert _kinds(changes) == [("modified", "scaled_total", "helper")]


def test_blank_line_between_functions_is_not_a_change(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"m.py": BILLING}, "base")
    head = commit_tree(git_repo, {"m.py": BILLING.replace("\n\ndef refund", "\n\n\ndef refund")}, "blank")
    _, changes = _changes(git_repo, base, head)
    assert changes == []
    commented = commit_tree(
        git_repo, {"m.py": BILLING.replace("RATE = 2", "RATE = 2  # per cent")}, "comment"
    )
    _, changes = _changes(git_repo, head, commented)
    assert _kinds(changes) == [("trivial", "<module>", "<module>")]


QUOTED_NAMES = ["pkg/tab\there.py", 'pkg/q"uote.py', "pkg/back\\slash.py", 'pkg/caf"é.py']


def test_git_quoted_paths_are_decoded(git_repo: Path) -> None:
    files = {name: "def f():\n    return 1\n" for name in QUOTED_NAMES}
    base = commit_tree(git_repo, {**files, 'pkg/old"name.py': BILLING}, "base")
    edited = {name: "def f():\n    return 2\n" for name in QUOTED_NAMES}
    commit_tree(git_repo, edited, "edit")
    git(git_repo, "mv", 'pkg/old"name.py', "pkg/new\tname.py")
    git(git_repo, "commit", "-q", "-m", "rename")
    head = git(git_repo, "rev-parse", "HEAD")
    diffs, changes = _changes(git_repo, base, head)
    assert sorted(diffs) == sorted([*QUOTED_NAMES, "pkg/new\tname.py"])
    for name in QUOTED_NAMES:
        assert (diffs[name].status, diffs[name].added, diffs[name].removed) == ("modified", {2}, {2})
    assert (diffs["pkg/new\tname.py"].status, diffs["pkg/new\tname.py"].previous_path) == (
        "renamed",
        'pkg/old"name.py',
    )
    assert {(c.kind, c.region.path) for c in changes if c.kind == "modified"} == {
        ("modified", name) for name in QUOTED_NAMES
    }


@pytest.mark.parametrize(
    ("quoted", "plain"),
    [
        ('"a/tab\\there.py"', "a/tab\there.py"),
        ('"b/q\\"uote.py"', 'b/q"uote.py'),
        ('"a/back\\\\slash.py"', "a/back\\slash.py"),
        ('"a/caf\\303\\251\\n.py"', "a/café\n.py"),
        ('"a/bad\\377.py"', "a/bad\udcff.py"),
        ("a/plain name.py", "a/plain name.py"),
        ('"', '"'),
    ],
)
def test_unquote_git_path(quoted: str, plain: str) -> None:
    assert _unquote_git_path(quoted) == plain


def _many_functions(prefix: str, count: int, salt: int) -> str:
    """``count`` near-identical 14-line functions; ``salt`` edits only the return line."""
    body = "".join(f"    x{k} = a * {k} + b\n" for k in range(12))
    return "".join(f"def {prefix}_{i}(a, b):\n{body}    return x0 + {salt}\n\n\n" for i in range(count))


def test_fuzzy_pass_is_bounded_on_large_refactors() -> None:
    old_src, new_src = _many_functions("old", 400, 0), _many_functions("new", 400, 1)
    diffs = {"a.py": FileDiff("a.py", "deleted"), "b.py": FileDiff("b.py", "added")}
    started = time.perf_counter()
    changes = changed_functions(PythonAdapter(), {"a.py": old_src}, {"b.py": new_src}, diffs)
    assert time.perf_counter() - started < 20  # was minutes when every pair was compared
    assert {c.kind for c in changes} == {"added", "deleted"}  # 160,000 candidate pairs: no fuzzy pass


def test_fuzzy_pass_narrows_to_same_file_or_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(diff_module, "FUZZY_MAX_PAIRS", 4)
    old_src = _many_functions("old", 3, 0) + _many_functions("keep", 1, 0)
    new_src = _many_functions("new", 3, 1) + _many_functions("keep", 1, 1)
    diffs = {"a.py": FileDiff("a.py", "deleted"), "b.py": FileDiff("b.py", "added")}
    changes = changed_functions(PythonAdapter(), {"a.py": old_src}, {"b.py": new_src}, diffs)
    modified = [k for k in _kinds(changes) if k[0] == "modified"]
    assert modified == [("modified", "keep_0", "keep_0")]  # 16 pairs > 4: only the same-name pair is kept
    monkeypatch.setattr(diff_module, "FUZZY_MAX_PAIRS", 100)
    changes = changed_functions(PythonAdapter(), {"a.py": old_src}, {"b.py": new_src}, diffs)
    assert sum(1 for c in changes if c.kind == "modified") == 4


def test_unrelated_classes_with_the_same_skeleton_are_not_renamed(git_repo: Path) -> None:
    invoice = (
        "class Invoice:\n    def total(self):\n        return 1\n\n    def tax(self):\n        return 2\n"
    )
    refund = (
        "class Refund:\n    def issue(self):\n        return 1\n\n    def void(self):\n        return 3\n"
    )
    base = commit_tree(git_repo, {"pkg/m.py": invoice}, "base")
    head = commit_tree(git_repo, {"pkg/m.py": refund}, "head")
    kinds = _kinds(_changes(git_repo, base, head)[1])
    assert ("deleted", "-", "Invoice") in kinds and ("added", "Refund", None) in kinds
    assert not any(k[0] in ("renamed", "moved") and k[1] == "Refund" for k in kinds)


def test_renamed_class_with_the_same_members_is_renamed(git_repo: Path) -> None:
    invoice = "class Invoice:\n    def total(self):\n        return 1\n"
    base = commit_tree(git_repo, {"pkg/m.py": invoice}, "base")
    head = commit_tree(git_repo, {"pkg/m.py": invoice.replace("Invoice", "Bill")}, "head")
    kinds = _kinds(_changes(git_repo, base, head)[1])
    assert ("renamed", "Bill", "Invoice") in kinds
    assert ("renamed", "Bill.total", "Invoice.total") in kinds


def test_trivial_stubs_under_new_names_do_not_pair(git_repo: Path) -> None:
    base = commit_tree(git_repo, {"pkg/m.py": "def foo(x):\n    pass\n"}, "base")
    head = commit_tree(git_repo, {"pkg/m.py": "def bar(x):\n    pass\n"}, "head")
    kinds = _kinds(_changes(git_repo, base, head)[1])
    assert sorted(kinds, key=str) == [("added", "bar", None), ("deleted", "-", "foo")]
