"""``coverset secrets rekey``: re-encrypt the database vault under a new ``COVERSET_SECRET_KEY``.

The vault (:mod:`coverset.db.vault`) seals the GitHub App ``/setup`` stored under a key derived from
``COVERSET_SECRET_KEY``. To rotate that key, run this command with the *new* key in the environment
and the *old* one in ``--old-key`` (or ``COVERSET_OLD_SECRET_KEY``, or at the hidden prompt, which keep
it out of ``ps`` and the shell history), then give every replica the new key. It opens every sealed
row with the old key and seals it again under the new one in one transaction: a row that opens with
neither key aborts the command and changes nothing; a row already under the new key is left alone,
so a second run is harmless. Between the command and the rollout a replica still on the old key
cannot open the App, and a ``/setup`` run there would seal a new App under the old key, so the
command says to roll the new key out next and not to use ``/setup`` before that.
"""

from __future__ import annotations

import click

from coverset.commands.common import load, prepare_db, run_async
from coverset.db.session import Database
from coverset.db.vault import Vault, VaultError, rekey
from coverset.logging import register_secret

__all__ = ["secrets_group"]


@click.group(name="secrets")
def secrets_group() -> None:
    """The secrets coverset keeps in its database (the GitHub App /setup stored)."""


@secrets_group.command("rekey")
@click.option(
    "--old-key",
    envvar="COVERSET_OLD_SECRET_KEY",
    prompt="Previous COVERSET_SECRET_KEY",
    hide_input=True,
    help="The COVERSET_SECRET_KEY the rows are sealed under now (also COVERSET_OLD_SECRET_KEY, or a prompt).",
)
def rekey_command(old_key: str) -> None:
    """Seal every stored secret again under the current COVERSET_SECRET_KEY (one transaction)."""
    register_secret(old_key)
    settings = load()
    try:
        current = Vault.from_settings(settings)
        previous = Vault(old_key)
    except VaultError as exc:
        raise click.ClickException(str(exc)) from None

    async def run() -> list[str]:
        await prepare_db(settings)
        database = Database.from_url(settings.database_url)
        try:
            async with database.session() as session:
                changed = await rekey(session, previous, current)
                if changed:
                    from coverset.web.audit import record_audit

                    record_audit(
                        session, None, "secrets_rekeyed", target_kind="settings", details={"rows": changed}
                    )
                await session.commit()
                return changed
        finally:
            await database.engine.dispose()

    try:
        changed = run_async(run)
    except VaultError as exc:
        raise click.ClickException(f"{exc}; nothing was changed") from None
    if changed:
        click.echo(
            f"re-encrypted {len(changed)} row(s) under the current COVERSET_SECRET_KEY: {', '.join(changed)}"
        )
        click.echo(
            "Now give every replica the new COVERSET_SECRET_KEY and restart it. Until then a replica "
            "on the old key cannot open the stored App (its webhooks answer 503), and a /setup run "
            "there would seal a new App under the old key: do not use /setup before the rollout."
        )
    else:
        click.echo(
            "nothing to re-encrypt: no stored secret, or all already under the current COVERSET_SECRET_KEY"
        )
