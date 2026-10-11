"""Paths and builders shared by the neutral-bundle tests."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import sys
from pathlib import Path

from coverset.lang.base import NeutralBundle
from coverset_model.io import READER_META_KEYS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NEUTRAL_TS = PROJECT_ROOT / "fixtures" / "bundles" / "neutral-ts"
MINIMAL = PROJECT_ROOT / "fixtures" / "bundles" / "minimal"
DEMO_APP = PROJECT_ROOT / "examples" / "demo-app"


def strip_reader_meta(bundle: NeutralBundle) -> NeutralBundle:
    """``bundle`` without the ``meta`` keys a reader fills in (``bundle``, ``convert``)."""
    return dataclasses.replace(
        bundle, meta={k: v for k, v in bundle.meta.items() if k not in READER_META_KEYS}
    )


def copy_fixture(dest: Path) -> Path:
    """A private, mutable copy of the synthetic TypeScript bundle directory."""
    return Path(shutil.copytree(NEUTRAL_TS / "bundle", dest))


def run_demo_plugin(dest: Path) -> Path:
    """Run pytest-coverset over a copy of the demo app; returns its ``--coverset-out`` directory."""
    repo = dest / "demo-app"
    shutil.copytree(DEMO_APP, repo, ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__"))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COVERAGE_", "COVERSET_"))}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(dest)})
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--hypothesis-seed=0",
         "--coverset-out", "out", "--coverset-source", "ledger"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    return repo / "out"
