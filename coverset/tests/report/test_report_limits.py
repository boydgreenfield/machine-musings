"""Which limits ``coverset report`` reads a bundle under: the user's own ``pytest --coverset``
directory under the local profile (no byte cap, only the files read are counted, its tarball never
extracted), an archive or a neutral bundle under the strict limits ingest uses unless ``--trusted``
is passed, a run in the facts store as already ingested; ingest itself keeps the strict limits."""

from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import sys
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from report_helpers import DemoData, FixtureRun

from coverset.cli import cli
from coverset.facts.convert import read_python_bundle
from coverset.facts.untrusted import BundleError, BundleLimitError, BundleLimits
from coverset.report import build_report
from coverset.report.load import (
    LOCAL_LIMITS,
    RUN_LIMITS,
    STRICT_LIMITS,
    TRUSTED_LIMITS,
    load_bundle,
    load_run,
    read_source,
)
from coverset_model.io import Limits

STRICT_BYTES = BundleLimits().max_bundle_bytes
OVER = STRICT_BYTES + 10 * 1024 * 1024


def _plugin_copy(fixture_out: FixtureRun, dest: Path) -> Path:
    return Path(shutil.copytree(fixture_out.out, dest, ignore=shutil.ignore_patterns("report")))


def _sparse(path: Path, size: int) -> None:
    """A file of ``size`` zero bytes that takes no disk space."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab") as fh:
        fh.truncate(size)


def _archive(src: Path, dest: Path, *, pad: int = 0) -> Path:
    """``src``'s files as a flat ``tar.gz`` (stored, not compressed, so a padding member of ``pad``
    zero bytes makes the archive itself that large)."""
    with tarfile.open(dest, "w:gz", compresslevel=0) as tar:
        for path in sorted(src.iterdir()):
            if path.is_file():
                tar.add(path, arcname=path.name)
        if pad:
            padding = dest.parent / "padding.bin"
            _sparse(padding, pad)
            tar.add(padding, arcname="padding.bin")
            padding.unlink()
    return dest


def _neutral(dest: Path, *, blank_lines: int = 0) -> Path:
    """A small TypeScript neutral bundle; ``blank_lines`` empty lines are appended to tests.jsonl
    (they count against the row cap)."""
    dest.mkdir(parents=True)
    manifest = {
        "format": "coverset-neutral",
        "version": 1,
        "language": "typescript",
        "producer": {"name": "x", "version": "1"},
        "created_at": "2026-10-11T12:00:00Z",
        "shard": 0,
        "meta": {},
    }
    (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    test = {"id": "t::a", "file": "t.ts", "line": 1, "outcome": "passed", "duration_s": 0.1}
    (dest / "tests.jsonl").write_text(json.dumps(test) + "\n" + "\n" * blank_lines, encoding="utf-8")
    files = {"path": "src/a.ts", "n_lines": 3, "statements": [1, 2, 3], "excluded": [], "branches": []}
    (dest / "files.jsonl").write_text(json.dumps(files) + "\n", encoding="utf-8")
    cov = {"test": "t::a", "file": "src/a.ts", "lines": [1, 2]}
    (dest / "coverage.jsonl").write_text(json.dumps(cov) + "\n", encoding="utf-8")
    (dest / "functions.jsonl").write_text("", encoding="utf-8")
    return dest


def _report(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(cli, ["report", *args])
    return result.exit_code, result.output


# -- the profiles --------------------------------------------------------------------------------


def test_the_local_profile() -> None:
    local = BundleLimits.local()
    assert local.profile == "local" and BundleLimits().profile == "strict"
    assert local.max_bundle_bytes == sys.maxsize
    assert local.max_coverage_cells == 100_000_000
    assert local.max_tests > BundleLimits().max_tests and local.max_paths > BundleLimits().max_paths
    assert local.max_shape_keys > BundleLimits().max_shape_keys
    assert local.gzip_cap(50 * 1024 * 1024) >= 1024**3
    # the neutral reader's matching profile: raised row caps, no wall-clock budget
    assert all(getattr(local, n) == getattr(Limits.local(), n) for n in Limits.__dataclass_fields__)
    assert Limits.local().max_seconds == float("inf") and Limits.local().max_rows > Limits().max_rows
    # per-value rules are not resource caps: they stay the strict profile's
    assert local.max_line_number == BundleLimits().max_line_number
    assert local.max_nodeid_chars == BundleLimits().max_nodeid_chars


def test_ingest_keeps_the_strict_limits() -> None:
    from coverset.config import Settings
    from coverset.orchestrator import runner
    from coverset.orchestrator.bundles import bundle_limits

    limits = bundle_limits(Settings.for_tests())
    assert limits == BundleLimits() and limits.profile == "strict"
    assert all(getattr(limits, n) == getattr(Limits(), n) for n in Limits.__dataclass_fields__)
    # every IngestJob the runner builds takes these limits
    assert "limits=bundle_limits(self.settings)" in inspect.getsource(runner)


# -- a pytest --coverset directory: local -------------------------------------------------------


def test_a_plugin_directory_over_the_strict_byte_cap_builds(fixture_out: FixtureRun, tmp_path: Path) -> None:
    out = _plugin_copy(fixture_out, tmp_path / "out")
    # What made the user's 394 MB directory fail: a tarball and a neutral copy of the same run
    # next to the raw files. None of them is read, so none of them counts.
    _sparse(out / "bundle.tar.gz", OVER)
    _sparse(out / "neutral" / "padding.bin", OVER)
    # A file that IS read, over the strict cap on its own: the local profile has no byte cap.
    _sparse(out / "invariant_links.jsonl", OVER)
    with pytest.raises(BundleLimitError, match="max_bundle_bytes of the strict limits") as caught:
        read_python_bundle(out)
    assert caught.value.cap == "max_bundle_bytes" and caught.value.value is not None
    assert caught.value.value > STRICT_BYTES

    read = read_source(out)
    assert read.kind == "python" and read.limits == LOCAL_LIMITS
    assert read.read_from.startswith("the raw files (") and "coverage.sqlite" in read.read_from
    unread = read.read_from.split("; ", 1)[1]
    assert "bundle.tar.gz" in unread and "neutral/" in unread and "not read" in unread
    assert read.bundle.meta["convert"]["dropped"]["invariant_links.jsonl.oversized_lines"] >= 1

    code, output = _report(str(out), "--repo-root", str(fixture_out.project), "--out", str(tmp_path / "r"))
    assert code == 0, output


def test_the_tarball_is_never_extracted_when_the_raw_files_are_there(
    fixture_out: FixtureRun, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = _plugin_copy(fixture_out, tmp_path / "out")
    _archive(out, tmp_path / "packed.tar.gz")
    shutil.move(tmp_path / "packed.tar.gz", out / "bundle.tar.gz")

    def refuse(*args: object, **kwargs: object) -> Path:
        raise AssertionError("extract_bundle was called")

    import coverset.facts.untrusted
    import coverset.report.load

    monkeypatch.setattr(coverset.report.load, "extract_bundle", refuse)
    monkeypatch.setattr(coverset.facts.untrusted, "extract_bundle", refuse)
    inp = load_bundle(out, repo_root=fixture_out.project)
    assert inp.meta["bundle_kind"] == "python" and inp.meta["limits"] == LOCAL_LIMITS
    assert "bundle.tar.gz" in inp.meta["read_from"] and "not read" in inp.meta["read_from"]
    build_report(inp, tmp_path / "r")


def test_without_coverage_sqlite_the_neutral_copy_is_read(fixture_out: FixtureRun, tmp_path: Path) -> None:
    out = _plugin_copy(fixture_out, tmp_path / "out")
    assert (out / "neutral" / "manifest.json").is_file()
    (out / "coverage.sqlite").unlink()
    _sparse(out / "bundle.tar.gz", 1024)  # junk: never opened, the neutral copy wins
    read = read_source(out)
    assert read.kind == "neutral" and read.limits == LOCAL_LIMITS
    assert read.read_from.startswith("neutral/ (the plugin's neutral copy")
    assert read.bundle.coverage


def test_a_directory_with_only_the_tarball_reads_it_as_an_archive(
    fixture_out: FixtureRun, tmp_path: Path
) -> None:
    only = tmp_path / "only"
    only.mkdir()
    _archive(fixture_out.out, only / "bundle.tar.gz")
    read = read_source(only)
    assert read.kind == "python" and read.limits == STRICT_LIMITS
    assert read.read_from.startswith("bundle.tar.gz, extracted: the raw files (")


# -- an archive: strict unless --trusted ---------------------------------------------------------


def test_an_archive_over_the_cap_is_refused_and_builds_with_trusted(
    fixture_out: FixtureRun, tmp_path: Path
) -> None:
    archive = _archive(fixture_out.out, tmp_path / "bundle.tar.gz", pad=OVER)
    assert archive.stat().st_size > STRICT_BYTES
    root = str(fixture_out.project)

    code, output = _report(str(archive), "--repo-root", root, "--out", str(tmp_path / "refused"))
    assert code != 0
    text = " ".join(output.split())
    assert f"bundle archive is {archive.stat().st_size} bytes (limit {STRICT_BYTES})" in text
    assert f"strict cap max_bundle_bytes = {STRICT_BYTES:,}" in text
    assert "pass --trusted" in text
    assert "A directory your own pytest --coverset wrote is never capped this way" in text
    assert not (tmp_path / "refused").exists()

    code, output = _report(str(archive), "--trusted", "--repo-root", root, "--out", str(tmp_path / "r"))
    assert code == 0, output
    inp = load_bundle(archive, repo_root=fixture_out.project, trusted=True)
    assert inp.meta["limits"] == TRUSTED_LIMITS
    assert inp.meta["read_from"].startswith("bundle.tar.gz, extracted: the raw files (")


def test_a_compare_base_archive_follows_the_same_rule(fixture_out: FixtureRun, tmp_path: Path) -> None:
    archive = _archive(fixture_out.out, tmp_path / "base.tar.gz", pad=OVER)
    args = [str(fixture_out.out), "--repo-root", str(fixture_out.project), "--compare", str(archive)]
    code, output = _report(*args, "--out", str(tmp_path / "refused"))
    assert code != 0 and "cannot read the base" in output and "--trusted" in output
    code, output = _report(*args, "--trusted", "--out", str(tmp_path / "r"))
    assert code == 0, output


# -- a neutral bundle: strict unless --trusted ---------------------------------------------------


def test_a_neutral_bundle_over_the_strict_row_caps(tmp_path: Path) -> None:
    rows = Limits().max_rows
    bundle = _neutral(tmp_path / "neutral", blank_lines=rows + 1)
    with pytest.raises(BundleLimitError) as caught:
        read_source(bundle)
    assert caught.value.cap == "max_rows" and caught.value.limit == rows
    assert f"more than {rows} lines" in str(caught.value)

    code, output = _report(str(bundle), "--out", str(tmp_path / "refused"))
    text = " ".join(output.split())
    assert code != 0 and f"strict cap max_rows = {rows:,}" in text and "--trusted" in text

    read = read_source(bundle, trusted=True)
    assert read.kind == "neutral" and read.limits == TRUSTED_LIMITS
    assert [t.nodeid for t in read.bundle.tests] == ["t::a"]
    code, output = _report(str(bundle), "--trusted", "--out", str(tmp_path / "r"))
    assert code == 0, output

    info = CliRunner().invoke(cli, ["bundle", "info", str(bundle)])
    assert "refused:" in info.output and "--trusted" in info.output
    info = CliRunner().invoke(cli, ["bundle", "info", "--trusted", "--json", str(bundle)])
    summary = json.loads(info.output)
    assert summary["fatal"] is None and summary["limits"] == "local"
    assert CliRunner().invoke(cli, ["bundle", "validate", str(bundle)]).exit_code == 1
    assert CliRunner().invoke(cli, ["bundle", "validate", "--trusted", str(bundle)]).exit_code == 0


def test_a_plain_bundle_error_keeps_its_message(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(BundleError) as caught:
        read_source(empty)
    assert not isinstance(caught.value, BundleLimitError)
    code, output = _report(str(empty), "--out", str(tmp_path / "r"))
    assert code != 0 and "missing coverage.sqlite" in output and "--trusted" not in output


# -- a run in the facts store: already ingested -------------------------------------------------


def test_a_run_from_the_facts_store_is_read_as_local(demo: DemoData) -> None:
    inp = asyncio.run(load_run(demo.settings, demo.pr1_run))
    assert inp.meta["limits"] == RUN_LIMITS and RUN_LIMITS.startswith("local")
