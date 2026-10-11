"""The README's generated regions equal their sources (``scripts/check_readme.py``).

The numbers paragraph, the inline numbers and the root README's coverset bullet are rendered from
``docs/numbers.json``; the three shapes tables are compared with ``coverset shapes FUNC --markdown``
on the default branch of a freshly seeded local demo (example values of observed classes compared
loosely: the demo's Hypothesis tests are not derandomized).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_check_readme() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_readme", ROOT / "scripts" / "check_readme.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_readme", module)
    spec.loader.exec_module(module)
    return module


check_readme = _load_check_readme()


def _report(findings: list) -> str:
    return "\n\n".join(
        f.diff() or f"{f.where}: expected {f.expected!r}, found {f.actual!r}" for f in findings
    )


def test_numbers_regions_match_numbers_json() -> None:
    findings = check_readme.check()
    assert not findings, _report(findings)


def test_every_placeholder_has_a_number() -> None:
    numbers = json.loads((ROOT / "docs" / "numbers.json").read_text(encoding="utf-8"))
    text = check_readme.render_numbers(numbers) + check_readme.render_bullet(numbers)
    assert "{" not in text and "}" not in text
    assert check_readme.cpu_phrase("0.125-2.0") == "up to 2"
    assert check_readme.ordinal(1) == "1st" and check_readme.ordinal(66) == "66th"


def test_fix_rewrites_a_stale_region(tmp_path: Path) -> None:
    readme = tmp_path / "README.md"
    root = tmp_path / "ROOT.md"
    readme.write_text(
        "intro\n\n<!-- numbers:start -->\nstale\n<!-- numbers:end -->\n\ncosts <!-- n:overhead_x -->9<!-- /n -->x\n",
        encoding="utf-8",
    )
    root.write_text("- old bullet <!-- coverset-bullet -->\n", encoding="utf-8")
    assert len(check_readme.check(readme=readme, root_readme=root)) == 3
    assert len(check_readme.check(readme=readme, root_readme=root, fix=True)) == 3
    assert check_readme.check(readme=readme, root_readme=root) == []
    assert "stale" not in readme.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def demo_data_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A local demo seeded the way the web tests' session fixture does it (database in the data dir)."""
    if shutil.which("uv") is None or shutil.which("git") is None:
        pytest.skip("the demo needs uv and git")
    from coverset.config import Settings
    from coverset.db.session import init_db
    from coverset.demo.flow import run_demo
    from coverset.forge.fake import FakeForge
    from coverset.forge.signing import TEST_WEBHOOK_SECRET
    from coverset.logging import clear_registered_secrets
    from coverset.orchestrator.bundles import IngestPool
    from coverset.orchestrator.runner import OrchestratorContext

    data_dir = tmp_path_factory.mktemp("readme-demo") / "data"
    settings = Settings.for_tests(
        data_dir=data_dir,
        database_url=f"sqlite+aiosqlite:///{data_dir / 'coverset.db'}",
        github_webhook_secret=TEST_WEBHOOK_SECRET,
        local_allow_webhook_runs=True,
    )

    async def scenario() -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        await init_db(settings)
        ctx = OrchestratorContext.from_settings(settings, ingest=IngestPool("thread", max_workers=4))
        ctx.forge = FakeForge(session_factory=ctx.db.sessionmaker)
        try:
            await run_demo(ctx, out=data_dir / "demo.git")
        finally:
            await ctx.close()
            assert ctx.ingest is not None
            ctx.ingest.close()

    asyncio.run(scenario())
    clear_registered_secrets()
    return data_dir


@pytest.mark.slow
def test_shapes_tables_match_coverset_shapes(demo_data_dir: Path) -> None:
    # Observed rows' example values come partly from non-derandomized Hypothesis tests in the demo
    # app, so they are compared loosely here; everything else must match byte for byte.
    findings = check_readme.check(data_dir=demo_data_dir, loose_examples=True)
    assert not findings, _report(findings)


def test_mask_observed_examples() -> None:
    table = "| `text:multi-unit` | yes | `'10m4s'` |\n| `text:decimal` | **no** | `st.just(\"1.5h\")` |"
    masked = check_readme.mask_observed_examples(table)
    assert masked.splitlines()[0] == "| `text:multi-unit` | yes | ... |"
    assert masked.splitlines()[1] == table.splitlines()[1]
