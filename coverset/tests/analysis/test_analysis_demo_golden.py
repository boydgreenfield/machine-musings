"""Golden test: pytest-coverset on the demo app, then the input-class catalogue (plan phase 4).

The demo app is copied into its own git root (so shape and coverage paths are both relative to
it) and run in a subprocess with the five README functions' files in ``--coverset-full-trace``, as
a PR that changes them would be. The documented input classes must come out missing with no test
at the sample cap, i.e. labelled "never exercised".
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import pytest
from analysis_helpers import GIT_ENV

from coverset.analysis.functions import index_functions_detailed
from coverset.analysis.shapes import NEVER_EXERCISED, Catalogue, FunctionInputCov, annotation_index
from coverset.lang.python import PythonAdapter

DEMO_APP = Path(__file__).parents[2] / "examples" / "demo-app"
NEVER_EXERCISED_CLASSES = {
    "validate_email": {"email:multi-label-domain"},
    "parse_duration": {"text:multi-letter-unit"},
    "allocate_cents": {"total_cents:negative", "weights:zero-element"},
    "merge_intervals": {"intervals:unsorted"},
}
FULL_TRACE = (
    "src/ledger/accounts/emailcheck.py",
    "src/ledger/durations/parse.py",
    "src/ledger/billing/allocate.py",
    "src/ledger/intervals/merge.py",
)

pytestmark = pytest.mark.slow


@pytest.fixture(scope="module")
def demo_input_cov(tmp_path_factory: pytest.TempPathFactory) -> dict[str, FunctionInputCov]:
    """Run the instrumented demo suite once and compute input-class coverage of the README functions."""
    pytest.importorskip("pytest_coverset")
    pytest.importorskip("hypothesis")
    if shutil.which("git") is None or not (DEMO_APP / "src").is_dir():
        pytest.skip("needs git and examples/demo-app")
    repo = tmp_path_factory.mktemp("demo") / "demo-app"
    shutil.copytree(DEMO_APP, repo, ignore=shutil.ignore_patterns(".venv", ".pytest_cache", "__pycache__"))
    subprocess.run(["git", "init", "-q"], cwd=repo, env=GIT_ENV, check=True)
    (repo / "full-trace.txt").write_text("\n".join(FULL_TRACE) + "\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTEST_", "COVERAGE_", "COVERSET_"))}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "--hypothesis-seed=0",
            "--coverset-out",
            "out",
            "--coverset-source",
            "ledger",
            "--coverset-full-trace",
            "full-trace.txt",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    bundle = PythonAdapter().read_bundle(repo / "out")
    observations = defaultdict(list)
    for item in bundle.shapes:
        observations[item.func].append(item)
    sampling = {s.func: s for s in bundle.sampling}
    sources = {path: (repo / path).read_text() for path in FULL_TRACE}
    catalogue = Catalogue(annotations=annotation_index(sources))
    result: dict[str, FunctionInputCov] = {}
    for path, source in sources.items():
        for detail in index_functions_detailed(path, source):
            region = detail.region
            if region.qualname in NEVER_EXERCISED_CLASSES:
                result[region.qualname] = catalogue.function_input_cov(
                    region, observations.get(region.triple, []), sampling.get(region.triple)
                )
    return result


@pytest.mark.parametrize("function", sorted(NEVER_EXERCISED_CLASSES))
def test_readme_input_classes_are_never_exercised(
    demo_input_cov: dict[str, FunctionInputCov], function: str
) -> None:
    cov = demo_input_cov[function]
    assert cov.observed, f"{function} was never traced"
    assert NEVER_EXERCISED_CLASSES[function] <= {c.name for c in cov.missing}
    assert cov.tests_truncated == 0
    assert cov.missing_reason == NEVER_EXERCISED
