"""Seeding the deterministic demo repository (a thin wrapper around ``scripts/seed_demo.py``).

The demo needs the source tree (``examples/demo-app``, ``examples/demo-prs`` and the seeding script),
so it only works from a coverset checkout; the script is loaded by path, so its SHAs (pinned in
``tests/test_seed_demo.py``) stay the single source of truth.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

__all__ = [
    "DEMO_FULL_NAME",
    "DEMO_INSTALLATION_ID",
    "DEMO_OWNER_ID",
    "DEMO_REPO_ID",
    "PROJECT_ROOT",
    "DemoIdentity",
    "demo_identity",
    "load_seed_module",
    "read_shas",
    "seed_demo_repo",
]

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SEED_SCRIPT = PROJECT_ROOT / "scripts" / "seed_demo.py"
PR_OPENED_FIXTURE = PROJECT_ROOT / "fixtures" / "webhooks" / "pr_opened.json"

DEMO_FULL_NAME = "coverset-demo/ledger"
DEMO_REPO_ID = 424242
DEMO_OWNER_ID = 777
DEMO_INSTALLATION_ID = 55


def load_seed_module() -> ModuleType:
    """Import ``scripts/seed_demo.py`` by path (raises RuntimeError outside a source checkout)."""
    if not SEED_SCRIPT.is_file():
        raise RuntimeError(f"the demo needs a coverset source checkout ({SEED_SCRIPT} not found)")
    name = "coverset_seed_demo_script"
    module = sys.modules.get(name)
    if module is not None:
        return module
    spec = importlib.util.spec_from_file_location(name, SEED_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SEED_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def seed_demo_repo(out: Path) -> dict[str, Any]:
    """(Re)create the bare demo repository at ``out``; returns the ``<out>.shas.json`` content.

    Refuses (ValueError) to replace a directory that is not a seeded demo repository.
    """
    module = load_seed_module()
    result = module.seed(out.resolve())
    return json.loads(result.to_json())


def read_shas(out: Path) -> dict[str, Any] | None:
    """The SHA manifest next to a seeded repository, or None."""
    path = out.with_name(out.name + ".shas.json")
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


class DemoIdentity:
    """How the demo repository presents itself to the forge (matches the webhook fixtures)."""

    def __init__(
        self, external_id: int, full_name: str, owner_id: int | None, installation_id: int | None
    ) -> None:
        self.external_id = external_id
        self.full_name = full_name
        self.owner_id = owner_id
        self.installation_id = installation_id


def demo_identity() -> DemoIdentity:
    """The repository identity of ``fixtures/webhooks/pr_opened.json``, else the documented defaults."""
    try:
        data = json.loads(PR_OPENED_FIXTURE.read_text(encoding="utf-8"))
        payload = json.loads(data["body"])
        repo = payload["repository"]
        return DemoIdentity(
            external_id=int(repo["id"]),
            full_name=str(repo["full_name"]),
            owner_id=int(repo["owner"]["id"]) if repo.get("owner") else DEMO_OWNER_ID,
            installation_id=int(payload["installation"]["id"]) if payload.get("installation") else None,
        )
    except (OSError, ValueError, KeyError, TypeError):
        return DemoIdentity(DEMO_REPO_ID, DEMO_FULL_NAME, DEMO_OWNER_ID, DEMO_INSTALLATION_ID)
