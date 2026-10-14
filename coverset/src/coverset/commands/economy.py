"""`coverset tests economy|redundant|minimal` (owner: test-economy agent)."""

from __future__ import annotations

import click


@click.group(name="economy", hidden=True)
def economy_group() -> None:
    """Placeholder group until the feature lands."""
