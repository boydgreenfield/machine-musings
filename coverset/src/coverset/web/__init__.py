"""HTTP surface of the control plane: blueprints, access control and request-scoped accessors."""

from __future__ import annotations

from quart import Quart

from coverset.web import api, auth, dashboard, files, outbox, pages, repos, runs, setup, webhooks


def register_blueprints(app: Quart) -> None:
    """Register every blueprint; ``auth`` first so its access guard runs before other hooks."""
    app.register_blueprint(auth.bp)
    app.register_blueprint(api.bp)
    app.register_blueprint(pages.bp)
    app.register_blueprint(dashboard.bp)
    app.register_blueprint(repos.bp)
    app.register_blueprint(runs.bp)
    app.register_blueprint(files.bp)
    app.register_blueprint(outbox.bp)
    app.register_blueprint(webhooks.bp)
    app.register_blueprint(setup.bp)
