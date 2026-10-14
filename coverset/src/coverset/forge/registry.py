"""Choose the forge adapter from configuration and map webhook route names to adapters.

``COVERSET_FORGE`` selects the adapter: ``fake`` (tests and the offline demo), ``github`` (App mode
when there is an App, else token mode with ``COVERSET_GITHUB_TOKEN``) or ``auto`` (the default:
``github`` when credentials exist, else ``fake``). The App comes from the environment or from the
row ``/setup`` stored (:mod:`coverset.forge.credentials`).

The app's adapter is a :class:`LiveForge`: it rebuilds the concrete adapter whenever the credentials
change (an App stored or forgotten by ``/setup`` in any replica), so no restart is needed.
``POST /webhooks/<name>`` resolves ``name`` through :func:`webhook_forge`: ``github`` is served by
whichever adapter is configured (the fake one parses the same GitHub-shaped payloads), ``fake`` only
while the fake is configured.
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Mapping
from typing import Any, Literal, cast

from quart import Quart

from coverset.config import Settings, UntrustedPrPolicy
from coverset.forge.base import Forge, ForgeError
from coverset.forge.credentials import CredentialCache, GitHubCredentials, effective_owners, from_environment
from coverset.forge.fake import FakeForge, SessionFactory
from coverset.forge.github import GitHubForge
from coverset.forge.signing import webhook_secret

log = logging.getLogger(__name__)

ForgeMode = Literal["github-app", "github-token", "fake"]
FORGE_ENV = "COVERSET_FORGE"
APP_EXTENSION = "forge"
CREDENTIALS_EXTENSION = "github_credentials"


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


def forge_mode(
    settings: Settings,
    environ: Mapping[str, str] | None = None,
    credentials: GitHubCredentials | None = None,
) -> ForgeMode:
    """Which adapter :func:`get_forge` builds for these settings and ``credentials`` (default: the
    environment's)."""
    choice = configured_choice(settings, environ)
    credentials = credentials if credentials is not None else from_environment(settings)
    has_app = credentials.app is not None
    has_token = settings.github_token is not None and bool(settings.github_token.get_secret_value())
    if choice == "fake" or (choice == "auto" and not (has_app or has_token)):
        return "fake"
    if has_app:
        return "github-app"
    if has_token:
        return "github-token"
    raise ValueError(
        f"{FORGE_ENV}=github needs a GitHub App (/setup, or COVERSET_GITHUB_APP_ID) or COVERSET_GITHUB_TOKEN"
    )


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
    credentials: GitHubCredentials | None = None,
    session_factory: SessionFactory | None = None,
    environ: Mapping[str, str] | None = None,
) -> Forge:
    """Build the adapter for ``credentials`` (default: the environment's); the fake one records
    calls through ``session_factory``."""
    credentials = credentials if credentials is not None else from_environment(settings)
    mode = forge_mode(settings, environ, credentials)
    if mode == "fake":
        secret, _problem = webhook_secret(settings, credentials)
        return FakeForge(webhook_secret=secret, session_factory=session_factory)
    if mode == "github-app" and not effective_owners(settings, credentials):
        # The environment's App needs the list at start-up (config.py); a stored one defaults to its
        # owner, and one without an owner must not accept every owner's events.
        raise ValueError("the stored GitHub App names no owner: set COVERSET_ALLOWED_OWNERS")
    return GitHubForge.from_settings(settings, credentials)


class LiveForge:
    """The configured adapter, rebuilt when the credentials change (see the module docstring).

    Coroutine methods refresh the credentials first (at most one indexed read every few seconds)
    and then call the adapter for them; everything else (``name``, ``allowed_events``,
    ``verify_webhook``, ``capabilities()``) is the adapter of the credentials last read, which the
    app refreshes before every request and the orchestrator through
    :meth:`~coverset.orchestrator.runner.OrchestratorContext.current_forge` before it reads them;
    until the first refresh it is the environment's adapter (the fake forge when the environment
    holds nothing). A misconfiguration (an App id without a private key,
    ``COVERSET_FORGE=github`` without credentials) raises :class:`~coverset.forge.base.ForgeError`
    from each call, so runs complete and only the posting fails.
    """

    def __init__(
        self, settings: Settings, credentials: CredentialCache, session_factory: SessionFactory | None
    ) -> None:
        self.settings = settings
        self.credentials = credentials
        self._session_factory = session_factory
        self._built_for: GitHubCredentials | None = None
        self._adapter: Forge | None = None

    def _build(self, credentials: GitHubCredentials) -> Forge:
        if self._adapter is None or credentials is not self._built_for:
            try:
                adapter = get_forge(
                    self.settings, credentials=credentials, session_factory=self._session_factory
                )
            except ValueError as exc:
                raise ForgeError(f"the forge is misconfigured: {exc}") from None
            self._adapter, self._built_for = adapter, credentials
        return self._adapter

    @property
    def adapter(self) -> Forge:
        """The adapter for the credentials last read (no database access)."""
        return self._build(self.credentials.snapshot)

    async def current(self) -> Forge:
        """The adapter for the current credentials (refreshed first)."""
        return self._build(await self.credentials.refresh())

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.adapter, name)
        if not inspect.iscoroutinefunction(target):
            return target

        async def call(*args: Any, **kwargs: Any) -> Any:
            return await getattr(await self.current(), name)(*args, **kwargs)

        return call


def credential_cache(app: Quart) -> CredentialCache:
    """The app's :class:`~coverset.forge.credentials.CredentialCache` (one per app)."""
    cache = app.extensions.get(CREDENTIALS_EXTENSION)
    if cache is None:
        cache = CredentialCache(app.extensions["settings"], app.extensions["db"].sessionmaker)
        app.extensions[CREDENTIALS_EXTENSION] = cache
    return cache


def forge_for_app(app: Quart) -> Forge:
    """The app's adapter: a :class:`LiveForge` kept in ``app.extensions["forge"]`` (tests may preset
    a concrete one)."""
    forge = app.extensions.get(APP_EXTENSION)
    if forge is None:
        settings = app.extensions["settings"]
        forge = LiveForge(settings, credential_cache(app), app.extensions["db"].sessionmaker)
        app.extensions[APP_EXTENSION] = forge
        if effective_untrusted_pr_policy(settings) != settings.untrusted_pr_policy:
            log.warning(
                "token mode has no Approve run button: untrusted pull requests run with the network "
                "blocked (COVERSET_UNTRUSTED_PR_POLICY=run_blocked_network). Set "
                "COVERSET_UNTRUSTED_PR_POLICY=approve to approve them with POST /runs/<id>/approve "
                "instead, or skip to never run them."
            )
    return cast(Forge, forge)  # a LiveForge stands in for the protocol through __getattr__


async def webhook_forge(app: Quart, route_name: str) -> Forge | None:
    """The concrete adapter for ``POST /webhooks/<route_name>`` (credentials refreshed first), or
    None for an unknown name. Raises :class:`~coverset.forge.base.ForgeError` when misconfigured."""
    forge = forge_for_app(app)
    if isinstance(forge, LiveForge):
        forge = await forge.current()
    if route_name == "github":
        return forge
    if route_name == "fake" and forge.name == "fake":
        return forge
    return None


WEBHOOK_ROUTE_NAMES: tuple[str, ...] = ("github", "fake")
"""Route names :func:`webhook_forge` can resolve (``fake`` only in fake mode)."""
