"""API tokens: ``cst_<prefix>_<secret>`` strings whose sha256 is stored in ``api_tokens``.

:func:`generate` makes one; :func:`verify` looks a raw token up (constant-time on the hash) and
returns the live row or None. Scopes: ``read`` (pages and the read API), ``write`` (approve,
quarantine, settings the user may change), ``upload`` (``POST /v1/bundles`` for the
token's repository), ``admin`` (everything the user could do). The raw token is shown once and
never logged: the redactor masks ``cst_`` strings.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import ApiToken, TokenKind

TOKEN_PATTERN: Final = re.compile(r"^cst_([A-Za-z0-9]{8})_([A-Za-z0-9_-]{32,})$")
"""``cst_<8 chars prefix>_<secret>``; the redactor uses ``cst_[A-Za-z0-9_-]{8,}`` to mask it."""
SCOPES: Final = frozenset({"read", "write", "upload", "admin"})
PREFIX_CHARS: Final = 8


def hash_token(raw: str) -> str:
    """sha256 hex of a whole raw secret: an API token (its prefix is public, the secret is not) or a
    session cookie id (``user_sessions.id``), so a database read-out cannot be replayed."""
    return hashlib.sha256(raw.encode()).hexdigest()


def generate() -> tuple[str, str, str]:
    """Return ``(raw_token, prefix, token_hash)`` for a new token."""
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    prefix = "".join(secrets.choice(alphabet) for _ in range(PREFIX_CHARS))
    raw = f"cst_{prefix}_{secrets.token_urlsafe(32)}"
    return raw, prefix, hash_token(raw)


def looks_like_token(raw: str) -> bool:
    return TOKEN_PATTERN.match(raw) is not None


def check_scopes(scopes: list[str]) -> list[str]:
    """Validate and normalise a scope list (raises ValueError on an unknown scope)."""
    cleaned = sorted({s.strip() for s in scopes if s.strip()})
    unknown = [s for s in cleaned if s not in SCOPES]
    if unknown:
        raise ValueError(f"unknown token scope(s): {', '.join(unknown)}")
    return cleaned


async def verify(session: AsyncSession, raw: str, *, now: datetime) -> ApiToken | None:
    """The live ``ApiToken`` row for ``raw`` (not revoked, not expired), or None.

    Looks the row up by prefix and compares the stored hash with :func:`hmac.compare_digest`, so a
    wrong secret costs the same time as a right one. Does not touch ``last_used_at`` (callers
    update it at most once a minute to keep writes rare).
    """
    match = TOKEN_PATTERN.match(raw)
    if match is None:
        return None
    prefix = match.group(1)
    digest = hash_token(raw)
    rows = (await session.execute(select(ApiToken).where(ApiToken.prefix == prefix))).scalars().all()
    for row in rows:
        if not hmac.compare_digest(row.token_hash.encode(), digest.encode()):
            continue
        if row.revoked_at is not None or (row.expires_at is not None and row.expires_at <= now):
            return None
        return row
    return None


async def create(
    session: AsyncSession,
    *,
    name: str,
    kind: TokenKind,
    scopes: list[str],
    now: datetime,
    user_id: int | None = None,
    repo_id: int | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, ApiToken]:
    """Insert an ``api_tokens`` row and return ``(raw_token, row)``; the caller commits.

    A ``repo`` token needs ``repo_id`` and acts on that repository alone; a ``personal`` token needs
    ``user_id`` and acts as that user. The raw token exists only in the return value.
    """
    cleaned = check_scopes(scopes)
    if not cleaned:
        raise ValueError("a token needs at least one scope")
    if kind == "repo" and repo_id is None:
        raise ValueError("a repository token needs a repository")
    if kind == "personal" and user_id is None:
        raise ValueError("a personal token needs a user")
    if not name.strip():
        raise ValueError("a token needs a name")
    raw, prefix, digest = generate()
    row = ApiToken(
        token_hash=digest,
        prefix=prefix,
        name=name.strip()[:255],
        kind=kind,
        user_id=user_id,
        repo_id=repo_id,
        scopes=cleaned,
        created_at=now,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return raw, row
