"""The audit log: who did what, written as ``audit_log`` rows.

:func:`record_audit` adds one :class:`~coverset.db.models.AuditEvent` to a caller's session (the
caller commits with the change it describes, so an event is never recorded for a change that
rolled back). :func:`audit_now` opens and commits its own session for events that belong to no
other transaction (a rejected token, a sign-in). Every string in ``details`` goes through the log
redactor before it is stored, so a raw token, a client secret or a private key that leaks into a
details dict is masked in the row; the audit page shows the rows to instance admins only.

Actions recorded by the account layer: ``login``, ``login_failed``, ``logout``, ``session_revoked``,
``token_rejected``, ``access_refreshed``, ``access_revoked``, ``role_changed``, ``user_disabled``,
``user_enabled``, ``token_created``, ``token_revoked``, ``credentials_written``; by the views:
``approve``, ``quarantine``; by the settings pages: ``setting_changed``.
"""

from __future__ import annotations

import logging
from typing import Any

from quart import has_app_context, has_request_context, request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import ActorKind, AuditEvent
from coverset.db.types import utc_now
from coverset.logging import redact
from coverset.web.context import app_db, app_settings
from coverset.web.principal import Principal, current_principal

log = logging.getLogger(__name__)

PAGE_LIMIT = 500
"""Rows the audit page lists (the newest first)."""
MAX_DETAIL_CHARS = 2000
"""Longest string kept in ``details`` (a longrepr or a stack would bloat the row)."""

_ACTOR_KIND: dict[str, ActorKind] = {
    "admin": "admin_token",
    "user": "user",
    "token": "api_token",
    "oidc": "oidc",
    "public": "system",
}


def actor_of(principal: Principal) -> tuple[ActorKind, str | None, str]:
    """``(actor_kind, actor_id, actor_label)`` for a principal."""
    kind = _ACTOR_KIND.get(principal.kind, "system")
    if principal.kind == "token" and principal.token_id is not None:
        actor_id: str | None = f"token:{principal.token_id}"
    elif principal.user_id is not None:
        actor_id = f"user:{principal.user_id}"
    elif principal.kind == "oidc" and principal.bound_repo_id is not None:
        actor_id = f"repo:{principal.bound_repo_id}"
    else:
        actor_id = None
    return kind, actor_id, (principal.label or principal.kind)[:255]


def scrub(details: Any) -> Any:
    """``details`` with every string redacted and truncated (nested lists and dicts included)."""
    if isinstance(details, str):
        return redact(details)[:MAX_DETAIL_CHARS]
    if isinstance(details, dict):
        return {str(key)[:64]: scrub(value) for key, value in details.items()}
    if isinstance(details, list | tuple):
        return [scrub(value) for value in details][:100]
    if isinstance(details, bool | int | float) or details is None:
        return details
    return redact(str(details))[:MAX_DETAIL_CHARS]


def _client_ip(explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit[:64]
    if has_request_context():
        return (request.remote_addr or None) and request.remote_addr[:64]
    return None


def record_audit(
    session: AsyncSession,
    principal: Principal | None,
    action: str,
    *,
    target_kind: str | None = None,
    target_id: str | int | None = None,
    repo_id: int | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditEvent:
    """Add one audit row to ``session`` (the caller commits). ``principal`` defaults to the current
    request's; pass ``None`` outside a request for a ``system`` actor (the CLI)."""
    who = principal if principal is not None else (current_principal() if has_request_context() else None)
    if who is None:
        kind, actor_id, label = "system", None, "cli"
    else:
        kind, actor_id, label = actor_of(who)
    event = AuditEvent(
        at=app_settings().now() if has_app_context() else utc_now(),
        actor_kind=kind,
        actor_id=actor_id,
        actor_label=label,
        action=action[:64],
        target_kind=target_kind[:32] if target_kind else None,
        target_id=str(target_id)[:128] if target_id is not None else None,
        repo_id=repo_id,
        ip=_client_ip(ip),
        details=scrub(details or {}),
    )
    session.add(event)
    return event


async def audit_now(
    action: str,
    *,
    principal: Principal | None = None,
    target_kind: str | None = None,
    target_id: str | int | None = None,
    repo_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record and commit one event in its own session; failures are logged, never raised, because
    an audit write must not turn a rejected request into a 500."""
    try:
        async with app_db().session() as session:
            record_audit(
                session,
                principal,
                action,
                target_kind=target_kind,
                target_id=target_id,
                repo_id=repo_id,
                details=details,
            )
            await session.commit()
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("audit event %s could not be written", action)


async def recent_events(
    session: AsyncSession,
    *,
    action: str | None = None,
    repo_id: int | None = None,
    limit: int = PAGE_LIMIT,
) -> list[AuditEvent]:
    """The newest ``limit`` events, optionally filtered by action and repository."""
    query = select(AuditEvent)
    if action:
        query = query.where(AuditEvent.action == action)
    if repo_id is not None:
        query = query.where(AuditEvent.repo_id == repo_id)
    rows = await session.scalars(query.order_by(AuditEvent.id.desc()).limit(limit))
    return list(rows)


async def known_actions(session: AsyncSession) -> list[str]:
    """Every distinct action in the log (for the filter)."""
    rows = await session.execute(select(AuditEvent.action).distinct().order_by(AuditEvent.action))
    return [action for (action,) in rows.all()]
