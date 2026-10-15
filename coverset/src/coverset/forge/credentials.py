"""Where the GitHub credentials come from: the environment, else the App ``/setup`` stored.

Exactly one source at a time:

* **The environment**, when any of :data:`ENVIRONMENT_NAMES` is set (``COVERSET_GITHUB_APP_ID``,
  ``..._CLIENT_ID``, ``..._CLIENT_SECRET``, ``..._PRIVATE_KEY`` as PEM text, ``COVERSET_GITHUB_TOKEN``,
  ``COVERSET_GITHUB_WEBHOOK_SECRET``): operators who keep their secrets in a platform's secret store.
  The stored row is then ignored and ``/setup`` is read-only (:class:`CredentialsInEnvironment`).
* **The vault** otherwise: the sealed ``github_app`` row of ``settings`` (:mod:`coverset.db.vault`,
  under ``COVERSET_SECRET_KEY``), written by ``/setup``'s manifest exchange (:func:`store_app`) and
  deleted by its "Forget the App" action (:func:`forget_app`).

:class:`CredentialCache` holds the current value per process. Environment credentials never touch
the database. The row is re-checked at most every :data:`REFRESH_S` seconds with one read of its
``updated_at``, and read and opened again only when that changed, so an App stored (or forgotten) in
one replica reaches every replica within seconds, without a restart. Every secret is registered
with the log redactor the moment it is loaded; none is ever logged.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import quote

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from coverset.config import Settings
from coverset.db.vault import (
    Vault,
    VaultError,
    delete_sealed,
    read_sealed,
    sealed_stamp,
    write_sealed,
)
from coverset.logging import register_secret

log = logging.getLogger(__name__)

APP_ROW = "github_app"
"""The ``settings`` row holding the sealed App."""
REFRESH_S = 5.0
"""Longest a process serves a stale view of the stored App (one indexed read per period)."""
GITHUB_WEB = "https://github.com"
ENVIRONMENT_NAMES: dict[str, str] = {
    "github_app_id": "COVERSET_GITHUB_APP_ID",
    "github_app_client_id": "COVERSET_GITHUB_APP_CLIENT_ID",
    "github_app_client_secret": "COVERSET_GITHUB_APP_CLIENT_SECRET",
    "github_app_private_key": "COVERSET_GITHUB_APP_PRIVATE_KEY",
    "github_token": "COVERSET_GITHUB_TOKEN",
    "github_webhook_secret": "COVERSET_GITHUB_WEBHOOK_SECRET",
}
"""Settings field -> environment name; any one set means the environment owns the GitHub secrets."""

Source = Literal["environment", "database", "none"]


class CredentialsExist(Exception):
    """An App is already stored; replacing it needs an explicit overwrite."""


class CredentialsInEnvironment(Exception):
    """The environment holds GitHub credentials, so nothing may be stored (the message names them)."""


@dataclass(frozen=True, slots=True)
class AppCredentials:
    """A GitHub App's identity and secrets (secrets hidden from ``repr``).

    From ``POST /app-manifests/{code}/conversions`` (:func:`coverset.forge.manifest.exchange_code`),
    from the vault, or from the environment (where only ``app_id`` and ``pem`` are certain).
    """

    app_id: int
    pem: str = field(repr=False)
    client_id: str | None = None
    client_secret: str | None = field(default=None, repr=False)
    webhook_secret: str | None = field(default=None, repr=False)
    slug: str | None = None
    owner: str | None = None
    html_url: str | None = None
    created_at: str | None = None
    """ISO 8601 UTC time ``/setup`` stored the App (None for environment credentials)."""

    @property
    def install_url(self) -> str | None:
        """Where to install the App on repositories (needs the slug)."""
        return f"{GITHUB_WEB}/apps/{quote(self.slug)}/installations/new" if self.slug else None

    def register_secrets(self) -> None:
        """Hand the client secret, webhook secret and private key to the log redactor."""
        for secret in (self.client_secret, self.webhook_secret, self.pem):
            register_secret(secret)

    def to_json(self) -> bytes:
        """The vault's plaintext: every field, as compact JSON."""
        return json.dumps(dataclasses.asdict(self), separators=(",", ":"), sort_keys=True).encode()

    @classmethod
    def from_json(cls, data: bytes) -> AppCredentials:
        """The inverse of :meth:`to_json`; :class:`VaultError` for anything else."""
        try:
            raw = json.loads(data)
            known = {f.name for f in dataclasses.fields(cls)}
            values = {k: v for k, v in raw.items() if k in known}
            app = cls(**values)
        except (ValueError, TypeError, AttributeError) as exc:
            raise VaultError(f"the stored GitHub App is not readable: {type(exc).__name__}") from None
        if not isinstance(app.app_id, int) or not isinstance(app.pem, str) or not app.pem:
            raise VaultError("the stored GitHub App has no app id or private key")
        return app


@dataclass(frozen=True, slots=True)
class GitHubCredentials:
    """The GitHub secrets in force, and where they came from."""

    source: Source
    app: AppCredentials | None = None
    """The App (App mode), or None (token mode, or the fake forge)."""
    webhook_secret: str | None = field(default=None, repr=False)
    names: tuple[str, ...] = ()
    """The environment names set (``source == "environment"``)."""
    stamp: datetime | None = None
    """``updated_at`` of the row these came from (``source == "database"``)."""
    problem: str | None = None
    """Why a stored row is not usable (no ``COVERSET_SECRET_KEY``, a different key, a damaged row)."""


NO_CREDENTIALS = GitHubCredentials("none")


def environment_names(settings: Settings) -> tuple[str, ...]:
    """The :data:`ENVIRONMENT_NAMES` that ``settings`` sets (empty values do not count)."""
    found = []
    for name, env_name in ENVIRONMENT_NAMES.items():
        value = getattr(settings, name)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if value not in (None, ""):
            found.append(env_name)
    return tuple(found)


def from_environment(settings: Settings) -> GitHubCredentials:
    """The environment's credentials, or :data:`NO_CREDENTIALS` when it sets none of them."""
    names = environment_names(settings)
    if not names:
        return NO_CREDENTIALS
    secret = settings.github_webhook_secret.get_secret_value() if settings.github_webhook_secret else ""
    app = None
    if settings.github_app_id is not None:
        key = settings.github_app_private_key.get_secret_value() if settings.github_app_private_key else ""
        client_secret = settings.github_app_client_secret
        app = AppCredentials(
            app_id=settings.github_app_id,
            pem=key,
            client_id=settings.github_app_client_id,
            client_secret=client_secret.get_secret_value() if client_secret else None,
            webhook_secret=secret or None,
        )
    return GitHubCredentials("environment", app=app, webhook_secret=secret or None, names=names)


async def load(session: AsyncSession, settings: Settings) -> GitHubCredentials:
    """The credentials in force: the environment's, else the stored App's, else none.

    A stored row that cannot be used is returned with :attr:`GitHubCredentials.problem` set and no
    App (the forge then behaves as if there were none, and ``/setup`` says why).
    """
    environment = from_environment(settings)
    if environment.source == "environment":
        return environment
    stamp = await sealed_stamp(session, APP_ROW)
    if stamp is None:
        return NO_CREDENTIALS
    try:
        found = await read_sealed(session, Vault.from_settings(settings), APP_ROW)
        if found is None:  # forgotten between the two reads
            return NO_CREDENTIALS
        app = AppCredentials.from_json(found[0])
    except VaultError as exc:
        return GitHubCredentials("database", stamp=stamp, problem=str(exc))
    app.register_secrets()
    return GitHubCredentials("database", app=app, webhook_secret=app.webhook_secret, stamp=found[1])


async def store_app(
    session: AsyncSession, settings: Settings, app: AppCredentials, *, overwrite: bool = False
) -> AppCredentials:
    """Seal ``app`` (stamped with ``created_at``) into the ``github_app`` row; the caller commits.

    Raises :class:`CredentialsInEnvironment` when the environment holds GitHub credentials,
    :class:`~coverset.db.vault.VaultKeyMissing` without ``COVERSET_SECRET_KEY``, and
    :class:`CredentialsExist` when a row exists and not ``overwrite`` (checked by the insert itself,
    so two concurrent first setups cannot both win).
    """
    names = environment_names(settings)
    if names:
        raise CredentialsInEnvironment(", ".join(names))
    vault = Vault.from_settings(settings)
    stamped = dataclasses.replace(app, created_at=datetime.now(UTC).isoformat(timespec="seconds"))
    written = await write_sealed(session, vault, APP_ROW, stamped.to_json(), replace=overwrite)
    if written is None:
        raise CredentialsExist(APP_ROW)
    stamped.register_secrets()
    return stamped


async def forget_app(session: AsyncSession) -> bool:
    """Delete the stored App (the caller commits); True when there was one."""
    return await delete_sealed(session, APP_ROW)


def effective_owners(settings: Settings, credentials: GitHubCredentials) -> list[str]:
    """``COVERSET_ALLOWED_OWNERS``, or, when it is empty, the stored App's own owner."""
    if settings.allowed_owners:
        return list(settings.allowed_owners)
    if credentials.source == "database" and credentials.app is not None and credentials.app.owner:
        return [credentials.app.owner]
    return []


def with_app_owner(settings: Settings, credentials: GitHubCredentials) -> Settings:
    """``settings`` whose allowed owners are :func:`effective_owners` (a copy only when they differ):
    an App stored by ``/setup`` accepts its own owner's events unless ``COVERSET_ALLOWED_OWNERS``
    says otherwise."""
    owners = effective_owners(settings, credentials)
    if owners == list(settings.allowed_owners):
        return settings
    return settings.model_copy(update={"allowed_owners": owners})


class CredentialCache:
    """The credentials in force for one process, refreshed cheaply (see the module docstring).

    :attr:`snapshot` is the last value read (what synchronous code uses inside a request: the app
    refreshes before each request); :meth:`refresh` re-checks the row when :data:`REFRESH_S` has
    passed; :meth:`invalidate` makes the next :meth:`refresh` check at once (after this process
    stored or forgot the App).
    """

    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession] | Callable[[], Any],
        *,
        refresh_s: float = REFRESH_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._sessionmaker = sessionmaker
        self._refresh_s = refresh_s
        self._clock = clock
        self.snapshot = from_environment(settings)
        self._checked: float | None = None

    @property
    def environment_owned(self) -> bool:
        """True when the environment owns the credentials (the database is never read)."""
        return self.snapshot.source == "environment"

    def invalidate(self) -> None:
        """Check the row on the next :meth:`refresh`."""
        self._checked = None

    async def refresh(self) -> GitHubCredentials:
        """The current credentials (at most one indexed read per :data:`REFRESH_S` seconds).

        A database error keeps the previous value and is logged without its details.
        """
        if self.environment_owned:
            return self.snapshot
        now = self._clock()
        if self._checked is not None and now - self._checked < self._refresh_s:
            return self.snapshot
        try:
            async with self._sessionmaker() as session:
                stamp = await sealed_stamp(session, APP_ROW)
                if self._checked is None or stamp != self.snapshot.stamp:
                    found = await load(session, self.settings)
                    if found.problem and found.problem != self.snapshot.problem:
                        log.error("the stored GitHub App is not usable: %s", found.problem)
                    if found.source != self.snapshot.source or found.stamp != self.snapshot.stamp:
                        log.info("GitHub credentials: %s", describe(found))
                    self.snapshot = found
        except Exception as exc:  # noqa: BLE001 - a blip must not fail the request that asked
            log.warning(
                "cannot re-read the stored GitHub App (%s); keeping the previous view", type(exc).__name__
            )
        self._checked = now
        return self.snapshot


def describe(credentials: GitHubCredentials) -> str:
    """One line for logs and pages, naming nothing secret."""
    if credentials.source == "environment":
        mode = f"App {credentials.app.app_id}" if credentials.app is not None else "no App"
        return f"from the environment ({', '.join(credentials.names)}): {mode}"
    if credentials.source == "database":
        if credentials.app is None:
            return f"a stored App that cannot be used: {credentials.problem}"
        name = credentials.app.slug or credentials.app.app_id
        return f"App {name} (id {credentials.app.app_id}) stored by /setup"
    return "none (no App stored, nothing in the environment)"
