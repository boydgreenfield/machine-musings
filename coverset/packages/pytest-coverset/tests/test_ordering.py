"""Priority ordering from a scores file, and nodeid lists passed through a pytest ``@args`` file.

Observed ``@file`` semantics (pytest 9.1.1, argparse ``fromfile_prefix_chars='@'``): one argument per
line, taken verbatim (no stripping, no comments, no quoting); items are collected in exactly the listed
order, even across modules; duplicate nodeids collapse to one item; non-ASCII parametrize ids must be
written the way pytest prints them (``\\xe9``); ``[``, ``]``, spaces, ``|`` and ``::`` inside ids need no
escaping; the file is read in text mode, so ``\\r\\n`` line ends are fine. Hazards: a blank line becomes
the argument ``""``, which collects the whole rootdir, and trailing whitespace makes the nodeid
unmatchable (usage error), so the runner must write ``\\n``-joined nodeids with no empty lines.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from support import read_jsonl

from pytest_coverset.ordering import load_scores

TWO_MODULES = {
    "test_a": """
        def test_a1():
            pass

        def test_a2():
            pass

        class TestA:
            def test_x(self):
                pass

            def test_y(self):
                pass
        """,
    "test_b": """
        def test_b1():
            pass

        def test_b2():
            pass
        """,
}


def collected(result: pytest.RunResult) -> list[str]:
    return [line for line in result.outlines if "::" in line and not line.startswith(("ERROR", "FAILED"))]


def test_file_order_without_scores(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(**TWO_MODULES)
    result = pytester.runpytest_subprocess("--collect-only", "-q")
    assert collected(result) == [
        "test_a.py::test_a1",
        "test_a.py::test_a2",
        "test_a.py::TestA::test_x",
        "test_a.py::TestA::test_y",
        "test_b.py::test_b1",
        "test_b.py::test_b2",
    ]


def test_scores_group_by_module_then_class(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(**TWO_MODULES)
    scores = {
        "test_b.py::test_b2": 5,
        "test_a.py::TestA::test_y": 3,
        "test_a.py::test_a1": 1,
        "test_a.py::test_a2": 1,
        "not_collected.py::test_gone": 99,
    }
    (pytester.path / "scores.json").write_text(json.dumps(scores))
    expected = [
        "test_b.py::test_b2",
        "test_b.py::test_b1",
        "test_a.py::TestA::test_y",
        "test_a.py::TestA::test_x",
        "test_a.py::test_a1",
        "test_a.py::test_a2",
    ]
    result = pytester.runpytest_subprocess("--collect-only", "-q", "--coverset-scores", "scores.json")
    assert collected(result) == expected
    pytester.runpytest_subprocess(
        "--coverset-scores", "scores.json", "--coverset-out", "out"
    ).assert_outcomes(passed=6)
    assert [row["nodeid"] for row in read_jsonl(pytester.path / "out" / "results.jsonl")] == expected


def test_ordering_respects_deselection(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(**TWO_MODULES)
    (pytester.path / "scores.json").write_text(json.dumps({"test_a.py::test_a2": 9, "test_b.py::test_b1": 1}))
    result = pytester.runpytest_subprocess(
        "--collect-only", "-q", "--coverset-scores", "scores.json", "-k", "not b1"
    )
    assert collected(result) == [
        "test_a.py::test_a2",
        "test_a.py::test_a1",
        "test_a.py::TestA::test_x",
        "test_a.py::TestA::test_y",
        "test_b.py::test_b2",
    ]


def test_bad_scores_file_is_a_usage_error(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_one="def test_one():\n    pass\n")
    (pytester.path / "scores.json").write_text("[1, 2]")
    result = pytester.runpytest_subprocess("--coverset-scores", "scores.json")
    assert result.ret == pytest.ExitCode.USAGE_ERROR


IDS_MODULE = """
import pytest

IDS = ["a[1]", "with space", "pipe|x", "é-ünï", "plain", "x::y", "]["]


@pytest.mark.parametrize("v", [pytest.param(i, id=name) for i, name in enumerate(IDS)])
def test_ids(v):
    pass


def test_other():
    pass
"""


def test_args_file_collects_exactly_the_listed_items_in_order(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_ids=IDS_MODULE, test_more="def test_m():\n    pass\n")
    everything = collected(pytester.runpytest_subprocess("--collect-only", "-q"))
    assert "test_ids.py::test_ids[\\xe9-\\xfcn\\xef]" in everything and len(everything) == 9
    wanted = [
        "test_ids.py::test_ids[plain]",
        "test_more.py::test_m",
        "test_ids.py::test_ids[pipe|x]",
        "test_ids.py::test_ids[\\xe9-\\xfcn\\xef]",
        "test_ids.py::test_ids[with space]",
        "test_ids.py::test_other",
        "test_ids.py::test_ids[a[1]]",
        "test_ids.py::test_ids[x::y]",
        "test_ids.py::test_ids[][]",
    ]
    (pytester.path / "args.txt").write_text("\n".join(wanted) + "\n", encoding="utf-8")
    assert collected(pytester.runpytest_subprocess("@args.txt", "--collect-only", "-q")) == wanted

    pytester.runpytest_subprocess("@args.txt", "--coverset-out", "out").assert_outcomes(passed=9)
    assert [row["nodeid"] for row in read_jsonl(pytester.path / "out" / "results.jsonl")] == wanted


def test_args_file_hazards(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(test_ids=IDS_MODULE, test_more="def test_m():\n    pass\n")
    (pytester.path / "blank.txt").write_text("test_more.py::test_m\n\ntest_ids.py::test_other\n")
    assert len(collected(pytester.runpytest_subprocess("@blank.txt", "--collect-only", "-q"))) == 9
    (pytester.path / "dups.txt").write_text("test_more.py::test_m\ntest_more.py::test_m\n")
    assert collected(pytester.runpytest_subprocess("@dups.txt", "--collect-only", "-q")) == [
        "test_more.py::test_m"
    ]
    (pytester.path / "crlf.txt").write_bytes(b"test_more.py::test_m\r\n")
    assert collected(pytester.runpytest_subprocess("@crlf.txt", "--collect-only", "-q")) == [
        "test_more.py::test_m"
    ]
    (pytester.path / "spaces.txt").write_text("test_more.py::test_m  \n")
    assert (
        pytester.runpytest_subprocess("@spaces.txt", "--collect-only", "-q").ret
        == pytest.ExitCode.USAGE_ERROR
    )


def test_load_scores_keeps_only_finite_numbers(tmp_path: Path) -> None:
    scores = tmp_path / "scores.json"
    scores.write_text(
        '{"a": NaN, "b": Infinity, "c": -Infinity, "d": 1e400, "e": 1' + "0" * 400 + ", "
        '"f": true, "g": "1", "h": 2, "i": 0.5}'
    )
    assert load_scores(scores) == {"h": 2.0, "i": 0.5}
