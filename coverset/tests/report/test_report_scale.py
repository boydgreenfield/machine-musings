"""A large repository: file pages list only their neighbours, never every file."""

from __future__ import annotations

from pathlib import Path

from coverset.lang.base import NeutralBundle, StaticFile, TestRow
from coverset.report import build_report
from coverset.report.build import FILE_LIST_WINDOW
from coverset.report.load import input_from_bundle


def test_file_list_is_windowed(tmp_path: Path) -> None:
    n = 2 * FILE_LIST_WINDOW + 50
    paths = [f"src/pkg/m{i:04d}.py" for i in range(n)]
    bundle = NeutralBundle(
        meta={},
        collected=(),
        tests=(
            TestRow(
                "tests/test_a.py::test_a", "tests/test_a.py", "passed", 0.1, None, None, None, None, None
            ),
        ),
        coverage={"tests/test_a.py::test_a": {p: (1,) for p in paths[:10]}},
        static={p: StaticFile(p, 2, (1, 2), (), ()) for p in paths},
        run_arcs={},
        shapes=(),
        sampling=(),
        durations={},
    )
    out = tmp_path / "out"
    inp = input_from_bundle(bundle, source=tmp_path / "bundle", kind="python", repo_root=tmp_path)
    result = build_report(inp, out)
    assert result.pages["files"] == n
    first = (out / f"files/{paths[0]}.html").read_text(encoding="utf-8")
    assert first.count('title="src/pkg/m') == FILE_LIST_WINDOW + 1
    assert f"… {n - FILE_LIST_WINDOW - 1} more files" in first
    middle = (out / f"files/{paths[n // 2]}.html").read_text(encoding="utf-8")
    assert middle.count('title="src/pkg/m') == 2 * FILE_LIST_WINDOW + 1
    assert "Source not available." in middle
