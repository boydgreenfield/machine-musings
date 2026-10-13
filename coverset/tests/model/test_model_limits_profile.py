"""The neutral reader's limit profiles as coverset passes them (``model_limits``) and as
``coverset bundle`` uses them (strict by default, local with ``--trusted``)."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from click.testing import CliRunner
from neutral_helpers import copy_fixture

from coverset.cli import cli
from coverset.facts.untrusted import BundleLimits
from coverset.lang.neutral import model_limits
from coverset_model.io import Limits, read_bundle


def test_model_limits_follow_the_profile() -> None:
    assert model_limits(None) == Limits() and model_limits(BundleLimits()) == Limits()
    assert model_limits(BundleLimits.local()) == Limits.local()
    # a field both share is taken from the BundleLimits; the rest from its profile
    tuned = model_limits(dataclasses.replace(BundleLimits.local(), max_tests=7))
    assert tuned.max_tests == 7 and tuned.max_rows == Limits.local().max_rows
    assert model_limits(dataclasses.replace(BundleLimits(), max_tests=7)).max_rows == Limits().max_rows


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
