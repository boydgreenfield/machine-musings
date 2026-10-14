"""Helpers shared by the agent tests (a module of its own: ``conftest`` is ambiguous when several
test directories have one)."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coverset.agent.tools import MAX_ROWS

RAW_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{2,}@[A-Za-z0-9-]+\.[A-Za-z.]+")
"""An email address whose local part was not masked (masked ones read ``a***@domain``)."""


@dataclass(frozen=True)
class DemoApp:
    """The measured demo app: its work tree and the ``.coverset`` directory."""

    root: Path
    bundle: Path


def walk(value: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    """Every ``(json path, value)`` of a JSON-like tree."""
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from walk(item, f"{path}[{index}]")


def assert_plain_and_capped(result: dict[str, Any]) -> None:
    """JSON-serialisable, and no list longer than :data:`MAX_ROWS`."""
    json.dumps(result)
    for where, value in walk(result):
        if isinstance(value, list):
            assert len(value) <= MAX_ROWS, f"{where} has {len(value)} rows"


def example_strings(result: dict[str, Any]) -> list[str]:
    """Every example value in a result (``example`` and ``repr`` fields)."""
    return [
        value
        for where, value in walk(result)
        if isinstance(value, str) and where.rsplit(".", 1)[-1] in ("example", "repr")
    ]
