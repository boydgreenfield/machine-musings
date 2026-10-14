"""`coverset doctor`: what coverset would do with a repository (owner: repo-compat agent)."""

from __future__ import annotations

import click


@click.group(name="doctor", hidden=True)
def doctor_group() -> None:
    """Placeholder group until the feature lands."""
