"""Accessors for the per-app objects that :func:`coverset.app.create_app` stores in extensions."""

from __future__ import annotations

from quart import current_app

from coverset.config import Settings
from coverset.db.session import Database


def app_settings() -> Settings:
    """The settings of the app handling the current request or app context."""
    return current_app.extensions["settings"]


def app_db() -> Database:
    """The database of the app handling the current request or app context."""
    return current_app.extensions["db"]
