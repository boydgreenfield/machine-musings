"""`coverset users ...` and `coverset tokens ...` (owner: auth agent)."""

from __future__ import annotations

import click


@click.group(name="users", hidden=True)
def users_group() -> None:
    """Placeholder group until the feature lands."""
