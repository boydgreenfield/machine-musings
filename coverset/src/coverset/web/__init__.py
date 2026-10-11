"""HTTP surface of the control plane: blueprints, access control and request-scoped accessors."""

from __future__ import annotations

from quart import Quart

from coverset.web import api, auth, home


def register_blueprints(app: Quart) -> None:
    """Register every blueprint; ``auth`` first so its access guard runs before other hooks."""
    app.register_blueprint(auth.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(home.bp)
