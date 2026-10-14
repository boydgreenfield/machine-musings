"""The neutral reader's limit profiles as coverset passes them (``BundleLimits``) and as
``coverset bundle`` uses them (strict by default, local with ``--trusted``)."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner
from neutral_helpers import copy_fixture

from coverset.cli import cli
from coverset.facts.untrusted import BundleLimits
from coverset_model.io import Limits, read_bundle


def test_bundle_limits_are_the_neutral_readers_limits() -> None:
    shared = Limits.__dataclass_fields__
    assert all(getattr(BundleLimits(), n) == getattr(Limits(), n) for n in shared)
    assert all(getattr(BundleLimits.local(), n) == getattr(Limits.local(), n) for n in shared)


def test_bundle_convert_takes_trusted(tmp_path: Path) -> None:
    bundle = copy_fixture(tmp_path / "bundle")
    with open(bundle / "tests.jsonl", "a") as fh:
        fh.write("\n" * (Limits().max_rows + 1))
    assert read_bundle(bundle, limits=Limits.local()).tests
    refused = CliRunner().invoke(cli, ["bundle", "convert", str(bundle), "--to", str(tmp_path / "a")])
    text = " ".join(refused.output.split())
    assert refused.exit_code != 0 and f"strict cap max_rows = {Limits().max_rows:,}" in text
    assert "pass --trusted" in text
    done = CliRunner().invoke(
        cli, ["bundle", "convert", "--trusted", str(bundle), "--to", str(tmp_path / "b")]
    )
    assert done.exit_code == 0, done.output
    info = json.loads(CliRunner().invoke(cli, ["bundle", "info", "--json", str(tmp_path / "b")]).output)
    assert info["limits"] == "strict" and info["fatal"] is None
