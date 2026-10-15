"""Shared helpers for the probe tests (imported by name; pytest puts this directory on sys.path)."""

from __future__ import annotations

from typing import Any


def rows_of(payload: dict[str, Any], qualname: str) -> list[dict[str, Any]]:
    """The payload's shape rows for one function (by qualname)."""
    index = {i for i, f in enumerate(payload["funcs"]) if f["qualname"] == qualname}
    return [row for row in payload["rows"] if row["f"] in index]


def func_of(payload: dict[str, Any], qualname: str) -> dict[str, Any]:
    """The payload's ``funcs`` entry of one function."""
    return next(f for f in payload["funcs"] if f["qualname"] == qualname)


def shapes_of(payload: dict[str, Any], qualname: str, param: str) -> dict[str, float]:
    """Shape key -> estimated calls for one parameter of one function."""
    return {row["s"]: row["calls"] for row in rows_of(payload, qualname) if row["p"] == param}
