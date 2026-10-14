"""`coverset mcp` stdio server (owner: MCP agent)."""

from __future__ import annotations

import click


@click.group(name="mcp", hidden=True)
def mcp_group() -> None:
    """Placeholder group until the feature lands."""
