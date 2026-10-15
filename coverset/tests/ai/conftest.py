"""Builders for AI-review tests over the demo app's README functions, and a hard network guard.

Every test in this package runs with outbound sockets disabled, so a stray live API call fails
loudly instead of leaving the machine.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from ai_helpers import (
    EMAIL_PATH,
    EMAIL_TESTS,
    BundleFactory,
    demo_invariants,
    demo_source,
    email_shapes,
    region_of,
)

from coverset.ai.bundle import build_review_bundle
from coverset.ai.schema import ReviewBundle
from coverset.analysis.shapes import Catalogue
from coverset.config import Settings
from coverset.lang.base import FunctionChange, ShapeObs


class NetworkBlocked(RuntimeError):
    """Raised by the socket guard: tests in tests/ai must never open a connection."""


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise NetworkBlocked("network access is disabled in tests/ai")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    yield


@pytest.fixture
def ai_settings(tmp_path: Path) -> Settings:
    """Hermetic settings (fake AI mode) on a temporary data dir."""
    return Settings.for_tests(data_dir=tmp_path / "data")


@pytest.fixture
def email_bundle(ai_settings: Settings) -> BundleFactory:
    """Build a bundle for ``validate_email`` with optional overrides of any input."""

    def build(
        *,
        source: str | None = None,
        tests: Sequence[tuple[str, str]] | None = None,
        shapes: Sequence[ShapeObs] | None = None,
        settings: Settings | None = None,
        kind: str = "modified",
        head_sha: str = "a" * 40,
        pr_number: int | None = 2,
        diff: str = "",
        extra: Mapping[str, Any] | None = None,
    ) -> ReviewBundle:
        text = source if source is not None else demo_source(EMAIL_PATH)
        region = region_of(EMAIL_PATH, text, "validate_email")
        observed = list(shapes) if shapes is not None else email_shapes(region)
        cov = Catalogue().function_input_cov(region, observed, None, annotations={"email": "str"})
        return build_review_bundle(
            repo_name="demo/ledger",
            head_sha=head_sha,
            pr_number=pr_number,
            changes=[FunctionChange(region=region, prev=region, kind=kind)],  # type: ignore[arg-type]
            sources={EMAIL_PATH: text},
            diff_hunks={EMAIL_PATH: diff} if diff else {},
            patch_rows=None,
            covering_tests={"validate_email": list(tests) if tests is not None else EMAIL_TESTS},
            input_covs={region.func_key: cov},
            shapes={region.func_key: observed},
            invariants=demo_invariants(),
            settings=settings or ai_settings,
            **(extra or {}),
        )

    return build
