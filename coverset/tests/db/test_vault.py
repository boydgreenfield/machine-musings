"""The database vault: AES-256-GCM rows of ``settings`` under a key derived from COVERSET_SECRET_KEY,
and ``coverset secrets rekey``."""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import SecretStr
from sqlalchemy import select

from coverset.cli import cli
from coverset.config import Settings
from coverset.db.models import AppSetting, AuditEvent
from coverset.db.session import Database
from coverset.db.vault import (
    ENVELOPE_VERSION,
    NONCE_BYTES,
    Vault,
    VaultError,
    VaultKeyMissing,
    read_sealed,
    rekey,
    write_sealed,
)

OLD_KEY = "old-secret-key-0123456789abcdef0123456789"
NEW_KEY = "new-secret-key-fedcba9876543210fedcba9876"
PLAINTEXT = b'{"pem": "-----BEGIN RSA PRIVATE KEY-----\\nMIIfake\\n-----END RSA PRIVATE KEY-----\\n"}'


def _flip(envelope: str, index: int) -> str:
    raw = bytearray(base64.b64decode(envelope))
    raw[index] ^= 0x01
    return base64.b64encode(bytes(raw)).decode()


@pytest.mark.security
def test_round_trip_and_the_envelope() -> None:
    vault = Vault(OLD_KEY)
    sealed = vault.seal("github_app", PLAINTEXT)
    raw = base64.b64decode(sealed)
    assert raw[0] == ENVELOPE_VERSION and len(raw) == 1 + NONCE_BYTES + len(PLAINTEXT) + 16
    assert b"BEGIN RSA" not in raw and OLD_KEY not in sealed
    assert vault.open("github_app", sealed) == PLAINTEXT
    assert Vault(OLD_KEY).open("github_app", sealed) == PLAINTEXT, "the key is derived, not stored"
    assert vault.seal("github_app", PLAINTEXT) != sealed, "a fresh nonce every time"
    assert "old-secret" not in repr(vault)


@pytest.mark.security
def test_the_wrong_key_does_not_open() -> None:
    sealed = Vault(OLD_KEY).seal("github_app", PLAINTEXT)
    with pytest.raises(VaultError, match="does not open with COVERSET_SECRET_KEY") as raised:
        Vault(NEW_KEY).open("github_app", sealed)
    assert "rekey" in str(raised.value)


@pytest.mark.security
@pytest.mark.parametrize("index", [0, 1, 1 + NONCE_BYTES, -1])
def test_tampered_ciphertext_is_refused(index: int) -> None:
    """The version byte, the nonce, the ciphertext and the tag are all covered."""
    vault = Vault(OLD_KEY)
    sealed = vault.seal("github_app", PLAINTEXT)
    with pytest.raises(VaultError):
        vault.open("github_app", _flip(sealed, index))
    with pytest.raises(VaultError, match="does not open"):
        vault.open("instance_id", sealed)  # the row name is associated data
    for junk in ("not base64!", base64.b64encode(b"\x01short").decode()):
        with pytest.raises(VaultError):
            vault.open("github_app", junk)


def test_no_secret_key_no_vault(tmp_path: Path) -> None:
    with pytest.raises(VaultKeyMissing, match="COVERSET_SECRET_KEY is not set"):
        Vault.from_settings(Settings.for_tests(data_dir=tmp_path))
    with pytest.raises(VaultKeyMissing):
        Vault("")
    assert Vault.from_settings(Settings.for_tests(data_dir=tmp_path, secret_key=SecretStr(OLD_KEY)))


async def test_rows_are_written_once_or_replaced(db: Database) -> None:
    vault = Vault(OLD_KEY)
    async with db.session() as session:
        first = await write_sealed(session, vault, "github_app", b"one", replace=False)
        assert first is not None
        assert await write_sealed(session, vault, "github_app", b"two", replace=False) is None
        await session.commit()
        found = await read_sealed(session, vault, "github_app")
        assert found is not None and found[0] == b"one"
        replaced = await write_sealed(session, vault, "github_app", b"three")
        await session.commit()
        found = await read_sealed(session, vault, "github_app")
        assert found is not None and found[0] == b"three" and replaced is not None and replaced > first
        with pytest.raises(ValueError, match="not a sealed settings row"):
            await write_sealed(session, vault, "instance_id", b"x")


@pytest.mark.security
async def test_rekey_reseals_every_row_in_one_transaction(db: Database) -> None:
    old, new = Vault(OLD_KEY), Vault(NEW_KEY)
    async with db.session() as session:
        await write_sealed(session, old, "github_app", PLAINTEXT)
        await session.commit()
    async with db.session() as session:
        assert await rekey(session, old, new) == ["github_app"]
        await session.commit()
    async with db.session() as session:
        value = await session.scalar(select(AppSetting.value).where(AppSetting.key == "github_app"))
        assert value is not None and new.open("github_app", value) == PLAINTEXT
        with pytest.raises(VaultError):
            old.open("github_app", value)
        assert await rekey(session, old, new) == [], "a second run changes nothing"
        stranger = Vault("a-third-key-that-sealed-nothing-0123456789")
        with pytest.raises(VaultError, match="opens with neither"):
            await rekey(session, stranger, Vault("yet-another-key-0123456789abcdef01234"))
        await session.rollback()
        value_after = await session.scalar(select(AppSetting.value).where(AppSetting.key == "github_app"))
        assert value_after == value, "a refused rekey leaves the row alone"


@pytest.mark.security
def test_rekey_command(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``coverset secrets rekey --old-key`` with the new key in the environment; audited, nothing
    secret printed or logged; a wrong old key changes nothing."""
    import asyncio

    async def seal_with_old() -> None:
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                await write_sealed(session, Vault(OLD_KEY), "github_app", PLAINTEXT)
                await session.commit()
        finally:
            await database.engine.dispose()

    asyncio.run(seal_with_old())
    monkeypatch.setenv("COVERSET_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("COVERSET_DATA_DIR", str(settings.data_dir))
    monkeypatch.setenv("COVERSET_SECRET_KEY", NEW_KEY)
    monkeypatch.delenv("COVERSET_OLD_SECRET_KEY", raising=False)
    runner = CliRunner()
    caplog.set_level(logging.DEBUG)
    wrong = runner.invoke(cli, ["secrets", "rekey", "--old-key", "not-the-old-key-0123456789abcdef"])
    assert (
        wrong.exit_code == 1
        and "opens with neither" in wrong.output
        and "nothing was changed" in wrong.output
    )
    done = runner.invoke(cli, ["secrets", "rekey"], input=OLD_KEY + "\n")  # the hidden prompt
    assert done.exit_code == 0, done.output
    assert "re-encrypted 1 row(s)" in done.output and "github_app" in done.output
    assert "do not use /setup before the rollout" in done.output, "the rekey window is spelled out"
    again = runner.invoke(cli, ["secrets", "rekey", "--old-key", OLD_KEY])
    assert again.exit_code == 0 and "nothing to re-encrypt" in again.output
    for text in (wrong.output, done.output, again.output, caplog.text):
        assert OLD_KEY not in text and NEW_KEY not in text and "MIIfake" not in text

    async def check() -> tuple[bytes, list[str]]:
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                found = await read_sealed(session, Vault(NEW_KEY), "github_app")
                actions = list(await session.scalars(select(AuditEvent.action)))
        finally:
            await database.engine.dispose()
        assert found is not None
        return found[0], actions

    plaintext, actions = asyncio.run(check())
    assert plaintext == PLAINTEXT and actions == ["secrets_rekeyed"]
