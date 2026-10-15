"""``POST /webhooks/<forge>``: the hardened webhook intake.

Order of checks (nothing is parsed or written before the signature is verified):

1. unknown forge name ⇒ 404; no usable webhook secret ⇒ 503 (fail closed; the fixtures' test
   secret counts as unusable outside ``COVERSET_ENV=test``); bodies over ``MAX_CONTENT_LENGTH``
   ⇒ 413 (before this view runs). The secret is the environment's ``COVERSET_GITHUB_WEBHOOK_SECRET``
   or the one of the App ``/setup`` stored, re-read within seconds of a change
   (:mod:`coverset.forge.credentials`);
2. ``X-Hub-Signature-256`` verified over the raw bytes ⇒ 403 on a missing or bad signature, with
   no part of the body echoed;
3. ``X-GitHub-Delivery`` must look like a GUID (400); a delivery already seen, by GUID or by the
   SHA-256 of its signed body (a captured delivery resent under a fresh GUID), answers 200 with
   no work;
4. events outside the adapter's allowlist (``forge.allowed_events``) are recorded and answered
   202 ``ignored_event``;
5. the payload is parsed (malformed ⇒ 400, delivery recorded as ``error``), handed to
   :func:`coverset.orchestrator.intake.intake`, and answered 202. Forge follow-ups (the "awaiting
   approval" check, the approver's permission check) and the run-queue hand-off happen after the
   response is ready.

Steps 3-5 run under one per-app :class:`asyncio.Lock`, so bursts
such as ``opened`` + ``ready_for_review`` for a new repo never race on the ``repos`` or
``pull_requests`` unique keys. If an :class:`~sqlalchemy.exc.IntegrityError` still escapes (another
process), the whole intake is retried once; a second failure is a 503 and nothing is half-stored.
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
from datetime import datetime

from quart import Blueprint, Response, current_app, jsonify, request
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import DeliveryStatus, ForgeKind, WebhookDelivery
from coverset.forge.base import ForgeError, ForgeEventError
from coverset.forge.credentials import with_app_owner
from coverset.forge.github_events import delivery_id, event_name
from coverset.forge.registry import credential_cache, webhook_forge
from coverset.forge.signing import webhook_secret
from coverset.orchestrator import enqueue_run
from coverset.orchestrator.intake import IntakeResult, apply_forge_followups, delivery_seen, intake
from coverset.web.context import app_db, app_extension, app_settings

log = logging.getLogger(__name__)

bp = Blueprint("webhooks", __name__)

INTAKE_LOCK_EXTENSION = "webhook_intake_lock"
INTAKE_ATTEMPTS = 2


def _intake_lock() -> asyncio.Lock:
    """The app's intake lock (kept per app so each test app and event loop gets its own)."""
    return app_extension(INTAKE_LOCK_EXTENSION, asyncio.Lock)


def _answer(code: int, /, **body: object) -> tuple[Response, int]:
    return jsonify(body), code


@bp.post("/webhooks/<forge_name>")
async def receive(forge_name: str) -> tuple[Response, int]:
    """Verify, deduplicate, record and act on one webhook delivery (see the module docstring)."""
    credentials = await credential_cache(current_app).refresh()
    settings = with_app_owner(app_settings(), credentials)
    try:
        forge = await webhook_forge(current_app, forge_name)
    except (ForgeError, ValueError) as exc:
        log.error("webhook forge is misconfigured: %s", exc)
        return _answer(503, error="forge_misconfigured")
    if forge is None:
        return _answer(404, error="not_found")
    _secret, problem = webhook_secret(settings, credentials)
    if problem is not None:
        if problem == "test_secret_outside_test":
            log.error("refusing webhooks: the fixtures' test secret is configured outside COVERSET_ENV=test")
        return _answer(503, error="webhook_secret_unset" if problem == "unset" else "webhook_secret_rejected")

    raw_body = await request.get_data(cache=False)
    headers = request.headers
    if not forge.verify_webhook(headers, raw_body):
        return _answer(403, error="bad_signature")

    try:
        delivery = delivery_id(headers)
    except ForgeEventError:
        return _answer(400, error="bad_delivery_id")
    name = event_name(headers)
    digest = hashlib.sha256(raw_body).hexdigest()
    received_at = settings.now()
    kind: ForgeKind = "fake" if forge_name == "fake" else "github"
    database = app_db()
    outcome: IntakeResult | None = None
    async with _intake_lock():
        for attempt in range(1, INTAKE_ATTEMPTS + 1):
            try:
                async with database.session() as session:
                    if await delivery_seen(session, delivery, digest):
                        return _answer(200, status="duplicate")
                    if name not in forge.allowed_events:
                        await _record(session, delivery, digest, kind, name, "ignored", received_at)
                        return _answer(202, status="ignored_event", reason="event not in allowlist")
                    try:
                        event = forge.parse_event(headers, raw_body)
                    except ForgeEventError as exc:
                        log.warning("malformed %s delivery %s: %s", name, delivery, exc)
                        await _record(session, delivery, digest, kind, name, "error", received_at)
                        return _answer(400, error="invalid_payload")
                    outcome = await intake(
                        session, settings, kind, event, received_at=received_at, body_digest=digest
                    )
                break
            except IntegrityError as exc:
                log.warning(
                    "delivery %s conflicted with a concurrent write (attempt %d): %s", delivery, attempt, exc
                )
    if outcome is None:
        log.error("delivery %s was not recorded after %d attempts", delivery, INTAKE_ATTEMPTS)
        return _answer(503, error="conflict")
    result = outcome

    if result.followups:
        current_app.add_background_task(
            apply_forge_followups,
            database.sessionmaker,
            forge,
            settings,
            result,
            on_queued=functools.partial(enqueue_run, current_app.extensions),
        )
    if result.should_enqueue and result.run_id is not None:
        await enqueue_run(current_app.extensions, result.run_id)
    body: dict[str, object] = {"status": result.status}
    if result.run_id is not None:
        body["run_id"] = result.run_id
    if result.reason:
        body["reason"] = result.reason
    return _answer(
        200 if result.status == "duplicate" and result.reason == "duplicate delivery" else 202, **body
    )


async def _record(
    session: AsyncSession,
    delivery: str,
    digest: str,
    forge: ForgeKind,
    name: str,
    status: DeliveryStatus,
    received_at: datetime,
) -> None:
    """Store a delivery that never reached intake (no payload kept)."""
    session.add(
        WebhookDelivery(
            delivery_id=delivery,
            body_sha256=digest,
            forge=forge,
            event=name[:64] or "(none)",
            status=status,
            payload=None,
            received_at=received_at,
        )
    )
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
