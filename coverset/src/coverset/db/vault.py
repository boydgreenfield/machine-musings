"""Encrypted rows in the ``settings`` table: the secrets coverset itself stores.

One row is sealed today: ``github_app``, the GitHub App ``/setup`` created (ids, client secret,
webhook secret, PEM private key; :mod:`coverset.forge.credentials`). Its value is a versioned envelope,
base64 text::

    version (1 byte, 0x01) | nonce (12 random bytes) | AES-256-GCM ciphertext and 16-byte tag

The key is derived with HKDF-SHA256 from ``COVERSET_SECRET_KEY`` (info ``coverset vault v1``, so it
never equals the cookie-signing key derived from the same setting), and the row's key name is the
associated data, so a sealed value copied under another name does not open. Without
``COVERSET_SECRET_KEY`` there is no vault: :meth:`Vault.from_settings` raises :class:`VaultKeyMissing`
with the fix in the message.

Rotation: ``coverset secrets rekey --old-key <old>`` (:func:`rekey`) opens every sealed row with the
old key and seals it again under the current one, in one transaction.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Sequence
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.config import Settings
from coverset.db.models import AppSetting
from coverset.db.types import utc_now

__all__ = [
    "ENVELOPE_VERSION",
    "SEALED_KEYS",
    "Vault",
    "VaultError",
    "VaultKeyMissing",
    "delete_sealed",
    "read_sealed",
    "rekey",
    "sealed_stamp",
    "write_sealed",
]

ENVELOPE_VERSION = 1
NONCE_BYTES = 12
KDF_INFO = b"coverset vault v1"
SEALED_KEYS: tuple[str, ...] = ("github_app",)
"""The ``settings`` rows that hold sealed values (what :func:`rekey` re-encrypts)."""
KEY_MISSING = (
    "COVERSET_SECRET_KEY is not set: coverset stores the GitHub App's credentials in the database "
    "encrypted under a key derived from it. Set it (at least 32 random characters, e.g. "
    "`openssl rand -hex 32`; the same value on every replica) and restart."
)


class VaultError(Exception):
    """A sealed value cannot be opened (wrong key, tampered or malformed) or written."""


class VaultKeyMissing(VaultError):
    """``COVERSET_SECRET_KEY`` is unset, so nothing can be sealed or opened."""


class Vault:
    """AES-256-GCM under a key derived from one secret (see the module docstring)."""

    def __init__(self, secret_key: str) -> None:
        if not secret_key:
            raise VaultKeyMissing(KEY_MISSING)
        key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=KDF_INFO).derive(
            secret_key.encode("utf-8")
        )
        self._aead = AESGCM(key)

    def __repr__(self) -> str:
        return "Vault(<key hidden>)"

    @classmethod
    def from_settings(cls, settings: Settings) -> Vault:
        """The vault of ``COVERSET_SECRET_KEY``; :class:`VaultKeyMissing` when it is unset."""
        secret = settings.secret_key.get_secret_value() if settings.secret_key else ""
        return cls(secret)

    def seal(self, name: str, plaintext: bytes) -> str:
        """``plaintext`` as an envelope (base64 text) bound to the row name ``name``."""
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._aead.encrypt(nonce, plaintext, name.encode("utf-8"))
        return base64.b64encode(bytes([ENVELOPE_VERSION]) + nonce + sealed).decode("ascii")

    def open(self, name: str, envelope: str) -> bytes:
        """The plaintext of an envelope made by :meth:`seal` for ``name``.

        Raises :class:`VaultError` for an unknown version, a malformed value, the wrong key or a
        changed byte (GCM's tag covers the ciphertext and the row name).
        """
        try:
            raw = base64.b64decode(envelope.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            raise VaultError(f"the sealed {name!r} row is not base64") from None
        if len(raw) < 1 + NONCE_BYTES + 16:
            raise VaultError(f"the sealed {name!r} row is too short")
        if raw[0] != ENVELOPE_VERSION:
            raise VaultError(f"the sealed {name!r} row has envelope version {raw[0]}, not {ENVELOPE_VERSION}")
        nonce, sealed = raw[1 : 1 + NONCE_BYTES], raw[1 + NONCE_BYTES :]
        try:
            return self._aead.decrypt(nonce, sealed, name.encode("utf-8"))
        except InvalidTag:
            raise VaultError(
                f"the sealed {name!r} row does not open with COVERSET_SECRET_KEY (a different key, or "
                "a changed value); after changing the key run `coverset secrets rekey --old-key ...`"
            ) from None


async def sealed_stamp(session: AsyncSession, name: str) -> datetime | None:
    """When the row ``name`` was last written, or None when there is none (one indexed read)."""
    return await session.scalar(select(AppSetting.updated_at).where(AppSetting.key == name))


async def read_sealed(session: AsyncSession, vault: Vault, name: str) -> tuple[bytes, datetime] | None:
    """The opened value of row ``name`` and when it was written, or None when there is no row."""
    row = await session.get(AppSetting, name, populate_existing=True)
    if row is None:
        return None
    return vault.open(name, row.value), row.updated_at


async def write_sealed(
    session: AsyncSession, vault: Vault, name: str, plaintext: bytes, *, replace: bool = True
) -> datetime | None:
    """Write row ``name`` with ``plaintext`` sealed (the caller commits); returns its ``updated_at``,
    which readers compare to notice the change. Without ``replace`` an existing row is kept and the
    result is None (one ``INSERT ... ON CONFLICT DO NOTHING``: of two concurrent writers one wins)."""
    if name not in SEALED_KEYS:
        raise ValueError(f"{name!r} is not a sealed settings row ({', '.join(SEALED_KEYS)})")
    now = utc_now()
    value = vault.seal(name, plaintext)
    statement = insert(AppSetting).values(key=name, value=value, updated_at=now)
    if replace:
        await session.execute(
            statement.on_conflict_do_update(index_elements=["key"], set_={"value": value, "updated_at": now})
        )
        return now
    inserted = await session.scalar(
        statement.on_conflict_do_nothing(index_elements=["key"]).returning(AppSetting.key)
    )
    return now if inserted is not None else None


async def delete_sealed(session: AsyncSession, name: str) -> bool:
    """Delete row ``name`` (the caller commits); True when there was one."""
    result = await session.execute(delete(AppSetting).where(AppSetting.key == name))
    return bool(getattr(result, "rowcount", 0))


async def rekey(
    session: AsyncSession, old: Vault, new: Vault, names: Sequence[str] = SEALED_KEYS
) -> list[str]:
    """Seal every row in ``names`` again under ``new`` after opening it with ``old``; returns the
    names re-encrypted. The rows are locked (``FOR UPDATE``) and the caller commits, so a failure
    on any row leaves every row as it was. A row that already opens with ``new`` (a second run) is
    left alone; one that opens with neither raises :class:`VaultError`."""
    rows = list(
        await session.scalars(
            select(AppSetting)
            .where(AppSetting.key.in_(list(names)))
            .order_by(AppSetting.key)
            .with_for_update()
        )
    )
    changed: list[str] = []
    for row in rows:
        try:
            plaintext = old.open(row.key, row.value)
        except VaultError:
            try:
                new.open(row.key, row.value)
            except VaultError:
                raise VaultError(
                    f"the sealed {row.key!r} row opens with neither --old-key nor COVERSET_SECRET_KEY"
                ) from None
            continue  # already under the current key (a second run)
        row.value = new.seal(row.key, plaintext)
        row.updated_at = utc_now()
        changed.append(row.key)
    return changed
