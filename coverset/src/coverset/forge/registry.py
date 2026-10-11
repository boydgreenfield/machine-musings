"""Choose the forge adapter from configuration and map webhook route names to adapters.

``COVERSET_FORGE`` selects the adapter: ``fake`` (tests and the offline demo), ``github`` (App mode
when ``COVERSET_GITHUB_APP_ID`` is set, else token mode with ``COVERSET_GITHUB_TOKEN``) or ``auto``
(the default: ``github`` when credentials exist, else ``fake``). ``POST /webhooks/<name>`` resolves
``name`` through :func:`webhook_forge`: ``github`` is served by whichever adapter is configured
(the fake one parses the same GitHub-shaped payloads), ``fake`` only while the fake is configured.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from typing import Literal

from quart import Quart

from coverset.config import Settings, UntrustedPrPolicy
from coverset.forge.base import Forge
from coverset.forge.fake import FakeForge, SessionFactory
from coverset.forge.github import GitHubForge
from coverset.forge.signing import webhook_secret

log = logging.getLogger(__name__)

ForgeMode = Literal["github-app", "github-token", "fake"]
FORGE_ENV = "COVERSET_FORGE"
APP_EXTENSION = "forge"


def configured_choice(settings: Settings, environ: Mapping[str, str] | None = None) -> str:
    """``auto``, ``github`` or ``fake``.

    ``Settings.forge`` wins when it is set to something other than ``auto``; with ``auto``
    an explicit ``COVERSET_FORGE`` in ``environ`` (a test or CLI override) still decides.
    """
    env = environ if environ is not None else os.environ
    value = getattr(settings, "forge", None)
    if value in (None, "", "auto"):
        value = env.get(FORGE_ENV)
    choice = (value or "auto").strip().lower()
    if choice not in ("auto", "github", "fake"):
        raise ValueError(f"{FORGE_ENV} must be auto, github or fake, got {value!r}")
    return choice


def forge_mode(settings: Settings, environ: Mapping[str, str] | None = None) -> ForgeMode:
    """Which adapter :func:`get_forge` builds for these settings."""
    choice = configured_choice(settings, environ)
    has_app = settings.github_app_id is not None
    has_token = settings.github_token is not None and bool(settings.github_token.get_secret_value())
    if choice == "fake" or (choice == "auto" and not (has_app or has_token)):
        return "fake"
    if has_app:
        return "github-app"
    if has_token:
        return "github-token"
    raise ValueError(f"{FORGE_ENV}=github needs COVERSET_GITHUB_APP_ID or COVERSET_GITHUB_TOKEN")


def effective_untrusted_pr_policy(
    settings: Settings, environ: Mapping[str, str] | None = None
) -> UntrustedPrPolicy:
    """``COVERSET_UNTRUSTED_PR_POLICY``, except that token mode defaults to ``run_blocked_network``.

    Token mode has no check-run buttons, so ``approve`` would leave untrusted PRs waiting on an
    admin's ``POST /runs/<id>/approve`` in coverset. Setting ``COVERSET_UNTRUSTED_PR_POLICY=approve``
    explicitly opts into that route; only the unset default changes.
    """
    policy = settings.untrusted_pr_policy
    if policy != "approve" or "untrusted_pr_policy" in settings.model_fields_set:
        return policy
    try:
        mode = forge_mode(settings, environ)
    except ValueError:
        return policy
    return "run_blocked_network" if mode == "github-token" else policy


def get_forge(
    settings: Settings,
    *,
    session_factory: SessionFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> Forge:
    """Build the configured adapter; the fake one records calls through ``session_factory``."""
    mode = forge_mode(settings, environ)
    if mode == "fake":
        secret, _problem = webhook_secret(settings)
        return FakeForge(webhook_secret=secret, session_factory=session_factory)
    return GitHubForge.from_settings(settings)


def forge_for_app(app: Quart) -> Forge:
    """The app's adapter, built once and kept in ``app.extensions["forge"]`` (tests may preset it)."""
    forge = app.extensions.get(APP_EXTENSION)
    if forge is None:
        database = app.extensions["db"]
        settings = app.extensions["settings"]
        forge = get_forge(settings, session_factory=database.sessionmaker)
        app.extensions[APP_EXTENSION] = forge
        if effective_untrusted_pr_policy(settings) != settings.untrusted_pr_policy:
            log.warning(
                "token mode has no Approve run button: untrusted pull requests run with the network "
                "blocked (COVERSET_UNTRUSTED_PR_POLICY=run_blocked_network). Set "
                "COVERSET_UNTRUSTED_PR_POLICY=approve to approve them with POST /runs/<id>/approve "
                "instead, or skip to never run them."
            )
    return forge


def webhook_forge(app: Quart, route_name: str) -> Forge | None:
    """The adapter for ``POST /webhooks/<route_name>``, or None for an unknown name."""
    forge = forge_for_app(app)
    if route_name == "github":
        return forge
    if route_name == "fake" and forge.name == "fake":
        return forge
    return None


WEBHOOK_ROUTE_NAMES: tuple[str, ...] = ("github", "fake")
"""Route names :func:`webhook_forge` can resolve (``fake`` only in fake mode)."""
