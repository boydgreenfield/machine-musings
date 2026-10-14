"""Machine-facing HTTP API (contract: plan Addendum A), served to ``coverset-ci wait`` and the UI.

``/v1/verdict``, ``/v1/runs/<id>`` and ``/v1/runs/<id>/events`` are public routes for the access
guard, so each view authorizes its caller itself (:func:`authorize_repo`):

* admin callers (open mode, admin bearer or login cookie) see everything;
* a signed-in user or a ``cst_`` token the access guard resolved reads the repositories it may
  read (``user`` access, treated like admin for reading); a ``cst_`` bearer bound elsewhere is a
  404, one the guard rejected a 401;
* a bearer that is not the admin token must be a GitHub Actions OIDC token whose
  ``repository_id`` and ``repository_owner_id`` match the repo (else 401 invalid / 404 mismatch);
* without a token, only repos recorded ``private=false`` from a verified webhook answer, limited to
  ``COVERSET_PUBLIC_VERDICT_RPM`` requests a minute per client address (0 disables this path).

Unknown and private repos are indistinguishable (404 ``not_found``).

Anonymous ("public") callers get a reduced view: ``/v1/verdict`` without ``summary``, run summaries
without the free-text reason, and only the Addendum A event kinds ``coverset-ci`` needs
(``progress``, ``test``, ``verdict``, ``done``) with their documented keys only: no ``log`` events,
``longrepr`` always null, nothing like ``delivery_id``, ``requested_by`` or an approver's login.
Their JSON polls are charged to a separate limiter (``2 x COVERSET_PUBLIC_VERDICT_RPM``), at most
:data:`ANON_SSE_PER_KEY` anonymous event streams run per client address and
:data:`ANON_SSE_TOTAL` in all, each for at most :data:`ANON_SSE_MAX_STREAM_S` (the client then
reconnects with ``Last-Event-ID``). OIDC bearers are limited per client address too
(:data:`BEARER_RPM`), before the token is verified.

Rate-limit keys are the client address. Behind a tunnel or reverse proxy that is the proxy's
address, so every anonymous caller shares one budget; coverset deliberately does not trust
``X-Forwarded-For`` (any caller can set it).

``POST /runs/<run_id>/approve`` lets an admin approve an untrusted run (token mode has no check
button; see :func:`coverset.orchestrator.intake.approve_awaiting_run`).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any, Literal

from quart import Blueprint, Response, current_app, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from werkzeug.exceptions import Unauthorized

from coverset import __version__
from coverset.db.models import Repo, Run, RunEvent, RunStatus, TestResult
from coverset.forge.oidc import OidcError, verify_actions_oidc
from coverset.forge.registry import forge_for_app
from coverset.orchestrator import enqueue_run
from coverset.orchestrator.intake import Followup, IntakeResult, apply_forge_followups, approve_awaiting_run
from coverset.web.auth import current_auth, is_admin
from coverset.web.context import app_db, app_settings
from coverset.web.principal import Principal, current_principal, require_repo, set_principal
from coverset.web.tokens import looks_like_token

bp = Blueprint("api", __name__)

Access = Literal["admin", "user", "oidc", "public"]

WEBHOOK_FORGES = ("github", "fake")
"""Repos created by verified webhooks; only these can be public or OIDC-bound."""

OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed", "not_run")
STATE_BY_STATUS: dict[str, str] = {
    "queued": "queued",
    "awaiting_approval": "queued",
    "paused": "queued",
    "running": "running",
}
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {"completed", "cancelled", "abandoned", "skipped", "error"}
)

SHA_PARAM = re.compile(r"^[0-9a-f]{40}$")
RUN_ID_PARAM = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REPO_ID_PARAM = re.compile(r"[0-9]{1,19}")
SEQ_PARAM = re.compile(r"-?[0-9]{1,18}")
INT64_MAX = 2**63 - 1

PUBLIC_EVENT_FIELDS: dict[str, tuple[str, ...]] = {
    "progress": ("done", "total", "passed", "failed", "shards", "elapsed_s"),
    "test": ("nodeid", "outcome", "duration_s", "failed_phase", "longrepr", "shard", "flaky"),
    "verdict": ("conclusion", "reason"),
    "done": ("conclusion", "exit_code", "counts"),
}
"""Event kinds (and their Addendum A keys) shown to anonymous callers; ``longrepr`` is nulled."""

EVENTS_PAGE = 500
PING_INTERVAL_S = 15.0
POLL_INTERVAL_S = 0.5
SSE_MAX_STREAM_S = 900.0
"""A stream ends after this long without a ``done`` event; clients reconnect with Last-Event-ID."""
ANON_SSE_MAX_STREAM_S = 120.0
"""The same limit for anonymous streams."""
ANON_SSE_PER_KEY = 4
ANON_SSE_TOTAL = 64
BEARER_RPM = 1200
"""Requests a minute per client address with a non-admin bearer (checked before verification)."""

JWK_CLIENT_EXTENSION = "oidc_jwk_client"
"""Optional ``app.extensions`` key: a JWK client to use instead of fetching GitHub's JWKS (tests)."""
LIMITER_EXTENSION = "public_verdict_limiter"
POLL_LIMITER_EXTENSION = "public_poll_limiter"
BEARER_LIMITER_EXTENSION = "bearer_limiter"
STREAM_SLOTS_EXTENSION = "anonymous_stream_slots"

Bucket = Literal["verdict", "poll", "stream"]


class ApiError(Exception):
    """An error answer: ``{"error": slug}`` with ``status``."""

    def __init__(self, status: int, slug: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(slug)
        self.status = status
        self.slug = slug
        self.headers = headers or {}


@bp.errorhandler(ApiError)
async def _api_error(error: ApiError) -> tuple[Response, int]:
    response = jsonify({"error": error.slug})
    response.headers.update(error.headers)
    if error.status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="coverset"'
    return response, error.status


class SlidingWindowLimiter:
    """At most ``per_minute`` hits per key in any 60 s window (``per_minute == 0`` denies all)."""

    def __init__(self, per_minute: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def hit(self, key: str) -> float | None:
        """Record a hit; None when allowed, else the seconds until the next hit would be."""
        if self.per_minute <= 0:
            return 60.0
        now = self._clock()
        window = self._hits.setdefault(key, deque())
        while window and now - window[0] >= 60.0:
            window.popleft()
        if len(window) >= self.per_minute:
            return max(1.0, 60.0 - (now - window[0]))
        window.append(now)
        if len(self._hits) > 10_000:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < 60.0}
        return None


class StreamSlots:
    """Counts open anonymous SSE streams per key and in total.

    A slot whose stream never released it (a generator that was never started) expires after
    ``ttl_s``, so a leak cannot lock the path shut.
    """

    def __init__(
        self,
        per_key: int = ANON_SSE_PER_KEY,
        total: int = ANON_SSE_TOTAL,
        *,
        ttl_s: float = ANON_SSE_MAX_STREAM_S + 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_key = per_key
        self.total = total
        self.ttl_s = ttl_s
        self._clock = clock
        self._open: dict[str, list[float]] = {}

    def _purge(self, now: float) -> None:
        for key in list(self._open):
            alive = [started for started in self._open[key] if now - started < self.ttl_s]
            if alive:
                self._open[key] = alive
            else:
                del self._open[key]

    def acquire(self, key: str) -> bool:
        """Take a slot for ``key``; False when the key or the whole path is at its cap."""
        now = self._clock()
        self._purge(now)
        held = self._open.get(key, [])
        if len(held) >= self.per_key or sum(len(v) for v in self._open.values()) >= self.total:
            return False
        self._open.setdefault(key, []).append(now)
        return True

    def release(self, key: str) -> None:
        """Give back the oldest slot of ``key`` (no-op when it already expired)."""
        held = self._open.get(key)
        if held:
            held.pop(0)
            if not held:
                del self._open[key]

    def count(self, key: str | None = None) -> int:
        """Open slots for ``key`` (or in total)."""
        self._purge(self._clock())
        if key is not None:
            return len(self._open.get(key, []))
        return sum(len(v) for v in self._open.values())


def _extension[T](name: str, build: Callable[[], T]) -> T:
    value = current_app.extensions.get(name)
    if value is None:
        value = current_app.extensions[name] = build()
    return value


def _limiter() -> SlidingWindowLimiter:
    return _extension(LIMITER_EXTENSION, lambda: SlidingWindowLimiter(app_settings().public_verdict_rpm))


def _poll_limiter() -> SlidingWindowLimiter:
    return _extension(
        POLL_LIMITER_EXTENSION, lambda: SlidingWindowLimiter(2 * app_settings().public_verdict_rpm)
    )


def _bearer_limiter() -> SlidingWindowLimiter:
    return _extension(BEARER_LIMITER_EXTENSION, lambda: SlidingWindowLimiter(BEARER_RPM))


def stream_slots() -> StreamSlots:
    """The app's anonymous SSE stream counter."""
    return _extension(STREAM_SLOTS_EXTENSION, StreamSlots)


def client_key() -> str:
    """The rate-limit key: the client address (a proxy's address behind a tunnel)."""
    return request.remote_addr or "unknown"


def _charge(limiter: SlidingWindowLimiter) -> None:
    retry_after = limiter.hit(client_key())
    if retry_after is not None:
        raise ApiError(429, "rate_limited", {"Retry-After": str(int(retry_after + 0.999))})


def _bearer() -> str | None:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None


async def authorize_repo(repo: Repo | None, *, bucket: Bucket = "verdict") -> Access:
    """Authorize the caller for ``repo`` (None = unknown); raises :class:`ApiError` (see module doc).

    ``bucket`` picks the anonymous limiter: ``poll`` for JSON event polling, else the verdict one.
    """
    if is_admin():
        if repo is None:
            raise ApiError(404, "not_found")
        return "admin"
    principal = current_principal()
    if repo is not None and principal.can(repo.id, "read"):
        return "user"
    token = _bearer()
    if token is not None:
        if looks_like_token(token):
            # A cst_ bearer the guard resolved but that cannot read this repository, or one the
            # guard rejected (a public route still reaches the view as ``public``).
            if principal.kind == "token":
                raise ApiError(404, "not_found")
            raise ApiError(401, "invalid_token")
        _charge(_bearer_limiter())
        settings = app_settings()
        kwargs: dict[str, Any] = {"audience": settings.oidc_audience}
        if (client := current_app.extensions.get(JWK_CLIENT_EXTENSION)) is not None:
            kwargs["jwk_client"] = client
        try:
            claims = await asyncio.to_thread(verify_actions_oidc, token, **kwargs)
        except OidcError:
            raise ApiError(401, "invalid_token") from None
        bound = (
            repo is not None
            and repo.forge in WEBHOOK_FORGES
            and repo.owner_id is not None
            and repo.external_id == str(claims.repository_id)
            and repo.owner_id == claims.repository_owner_id
        )
        if not bound:
            raise ApiError(404, "not_found")
        assert repo is not None
        set_principal(Principal(kind="oidc", label=f"actions job of {repo.full_name}", bound_repo_id=repo.id))
        return "oidc"
    _charge(_poll_limiter() if bucket == "poll" else _limiter())
    if repo is None or repo.private or repo.forge not in WEBHOOK_FORGES:
        raise ApiError(404, "not_found")
    return "public"


def run_state(run: Run) -> str:
    """Addendum A ``state``: queued, running or completed."""
    return STATE_BY_STATUS.get(run.status, "completed")


def run_conclusion(run: Run) -> str | None:
    """Addendum A ``conclusion`` (None until finished; errors and skips are ``neutral``)."""
    if run.verdict is not None:
        return run.verdict
    if run.status in ("cancelled", "abandoned"):
        return "cancelled"
    if run.status in ("error", "skipped", "completed"):
        return "neutral"
    return None


def summary_url(run: Run) -> str:
    """Absolute URL of the run page."""
    return f"{app_settings().public_url}/runs/{run.id}"


async def run_counts(session: AsyncSession, run_id: str) -> dict[str, int]:
    """Test outcome counts for a run (every outcome key present)."""
    rows = await session.execute(
        select(TestResult.outcome, func.count())
        .where(TestResult.run_id == run_id)
        .group_by(TestResult.outcome)
    )
    counts = dict.fromkeys(OUTCOMES, 0)
    for outcome, count in rows.all():
        counts[outcome] = int(count)
    return counts


def run_summary(run: Run, counts: dict[str, int], *, with_reason: bool = True) -> str:
    """One line describing the run (``with_reason=False`` leaves out the free-text reason)."""
    if run.status == "awaiting_approval":
        return "awaiting approval: untrusted pull request"
    if run.status == "paused":
        return "queued while coverset is paused"
    if run.status in ("queued", "running"):
        return run.status
    ran = ", ".join(f"{counts[k]} {k.replace('_', ' ')}" for k in OUTCOMES if counts.get(k)) or "no tests"
    head = f"{run_conclusion(run)}: {ran}"
    return f"{head} ({run.reason})" if run.reason and with_reason else head


@bp.get("/healthz")
async def healthz() -> Response:
    """Liveness probe: ``{"status": "ok", "version": "<coverset version>"}``."""
    return jsonify({"status": "ok", "version": __version__})


async def _repo_for(session: AsyncSession, repo_id: int) -> Repo | None:
    repo = await session.scalar(
        select(Repo)
        .where(Repo.external_id == str(repo_id), Repo.forge.in_(WEBHOOK_FORGES))
        .order_by(Repo.forge.desc())
        .limit(1)
    )
    if repo is None and is_admin():
        repo = await session.get(Repo, repo_id)
    return repo


@bp.get("/v1/verdict")
async def verdict() -> Response:
    """``?repo_id=<forge repository id>&sha=<40hex>`` → the newest run for that commit.

    ``repo_id`` is the forge's numeric repository id (GitHub ``repository.id``, what
    ``github.repository_id`` gives a workflow); admins may also pass coverset's own repo id.
    The first answer for a run records ``runs.ci_first_contact_at``.
    """
    repo_id = _repo_id_arg(request.args.get("repo_id", ""))
    sha = request.args.get("sha", "")
    if not SHA_PARAM.fullmatch(sha):
        raise ApiError(400, "bad_request")
    async with app_db().session() as session:
        repo = await _repo_for(session, repo_id)
        access = await authorize_repo(repo)
        assert repo is not None
        run = await session.scalar(
            select(Run)
            .where(Run.repo_id == repo.id, Run.head_sha == sha)
            .order_by(Run.created_at.desc())
            .limit(1)
        )
        if run is None:
            raise ApiError(404, "not_found")
        if run.ci_first_contact_at is None:
            run.ci_first_contact_at = app_settings().now()
            await session.commit()
        body: dict[str, Any] = {
            "state": run_state(run),
            "conclusion": run_conclusion(run),
            "run_id": run.id,
            "summary_url": summary_url(run),
        }
        if access != "public":
            body["summary"] = run_summary(run, await run_counts(session, run.id))
    return jsonify(body)


def _repo_id_arg(raw: str) -> int:
    """A forge repository id: 1-19 ASCII digits within int64 (``str.isdigit`` accepts ``²``)."""
    if not REPO_ID_PARAM.fullmatch(raw) or int(raw) > INT64_MAX:
        raise ApiError(400, "bad_request")
    return int(raw)


async def _load_run(session: AsyncSession, run_id: str, *, bucket: Bucket = "verdict") -> tuple[Run, Access]:
    if not RUN_ID_PARAM.fullmatch(run_id):
        raise ApiError(404, "not_found")
    run = await session.get(Run, run_id)
    repo = await session.get(Repo, run.repo_id) if run is not None else None
    access = await authorize_repo(repo, bucket=bucket)
    if run is None:
        raise ApiError(404, "not_found")
    return run, access


@bp.get("/v1/runs/<run_id>")
async def run_detail(run_id: str) -> Response:
    """Addendum A run resource: state, conclusion, outcome counts and a one-line summary."""
    async with app_db().session() as session:
        run, access = await _load_run(session, run_id)
        counts = await run_counts(session, run.id)
        return jsonify(
            {
                "run_id": run.id,
                "state": run_state(run),
                "conclusion": run_conclusion(run),
                "counts": counts,
                "summary": run_summary(run, counts, with_reason=access != "public"),
                "summary_url": summary_url(run),
            }
        )


def _event_json(event: RunEvent, *, public: bool = False) -> dict[str, Any]:
    payload = dict(event.payload or {})
    if public:
        payload = public_payload(event.kind, payload)
    return {"seq": event.seq, "kind": event.kind, "payload": payload}


def public_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The Addendum A keys of a public event kind, with ``longrepr`` nulled."""
    fields = PUBLIC_EVENT_FIELDS.get(kind, ())
    reduced = {key: payload[key] for key in fields if key in payload}
    if "longrepr" in fields:
        reduced["longrepr"] = None
    return reduced


async def _events_after(
    session: AsyncSession, run_id: str, after: int, limit: int, *, public: bool = False
) -> list[RunEvent]:
    query = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after)
    if public:
        query = query.where(RunEvent.kind.in_(tuple(PUBLIC_EVENT_FIELDS)))
    rows = await session.scalars(query.order_by(RunEvent.seq).limit(limit))
    return list(rows)


async def _has_done(session: AsyncSession, run_id: str) -> bool:
    found = await session.scalar(
        select(RunEvent.id).where(RunEvent.run_id == run_id, RunEvent.kind == "done").limit(1)
    )
    return found is not None


async def _synthetic_done(session: AsyncSession, run: Run) -> dict[str, Any]:
    """A ``done`` event for a run that ended without one (cancelled, abandoned, errored)."""
    last = await session.scalar(select(func.max(RunEvent.seq)).where(RunEvent.run_id == run.id))
    counts = await run_counts(session, run.id)
    conclusion = run_conclusion(run) or "neutral"
    exit_code = {"success": 0, "failure": 1}.get(conclusion, 2)
    return {
        "seq": (last if last is not None else -1) + 1,
        "kind": "done",
        "payload": {"conclusion": conclusion, "exit_code": exit_code, "counts": counts},
    }


def _seq_arg(value: str | None, default: int) -> int:
    """An event sequence number (``since``, ``Last-Event-ID``): optional sign, 1-18 ASCII digits."""
    if value in (None, ""):
        return default
    if not SEQ_PARAM.fullmatch(value):  # type: ignore[arg-type]
        raise ApiError(400, "bad_request")
    return int(value)  # type: ignore[arg-type]


@bp.get("/v1/runs/<run_id>/events")
async def run_events(run_id: str) -> Response:
    """JSON polling (``?since=<seq>``) or, with ``Accept: text/event-stream``, an SSE stream that
    honours ``Last-Event-ID``, pings every 15 s and ends after the ``done`` event."""
    wants_sse = "text/event-stream" in request.headers.get("Accept", "")
    since = _seq_arg(request.args.get("since"), -1)
    if wants_sse:
        since = _seq_arg(request.headers.get("Last-Event-ID"), since)
    database = app_db()
    async with database.session() as session:
        run, access = await _load_run(session, run_id, bucket="stream" if wants_sse else "poll")
        public = access == "public"
        if not wants_sse:
            rows = await _events_after(session, run.id, since, EVENTS_PAGE + 1, public=public)
            events = [_event_json(event, public=public) for event in rows[:EVENTS_PAGE]]
            more = len(rows) > EVENTS_PAGE
            done = not more and await _has_done(session, run.id)
            if not more and not done and run.status in TERMINAL_STATUSES:
                synthetic = await _synthetic_done(session, run)
                if synthetic["seq"] > since:
                    events.append(synthetic)
                done = True
            return jsonify({"events": events, "done": done})
    slot: tuple[StreamSlots, str] | None = None
    if public:
        slots, key = stream_slots(), client_key()
        if not slots.acquire(key):
            raise ApiError(429, "too_many_streams", {"Retry-After": "5"})
        slot = (slots, key)
    response = Response(
        _sse_stream(database.sessionmaker, run.id, since, public=public, slot=slot),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
    response.timeout = None
    return response


def _frame(event: dict[str, Any]) -> str:
    data = json.dumps(event["payload"], separators=(",", ":"))
    return f"id: {event['seq']}\nevent: {event['kind']}\ndata: {data}\n\n"


async def _sse_stream(
    session_factory: Callable[[], AsyncSession],
    run_id: str,
    since: int,
    *,
    public: bool = False,
    slot: tuple[StreamSlots, str] | None = None,
) -> AsyncIterator[bytes]:
    """Stored events after ``since``, then new ones as they land; ends after ``done`` (or after the
    stream's lifetime, when the client reconnects with ``Last-Event-ID``). Releases ``slot``."""
    try:
        async for chunk in _sse_events(session_factory, run_id, since, public=public):
            yield chunk
    finally:
        if slot is not None:
            slot[0].release(slot[1])


async def _sse_events(
    session_factory: Callable[[], AsyncSession], run_id: str, since: int, *, public: bool
) -> AsyncIterator[bytes]:
    started = last_ping = time.monotonic()
    lifetime = ANON_SSE_MAX_STREAM_S if public else SSE_MAX_STREAM_S
    last = since
    yield b": coverset event stream\n\n"
    while True:
        async with session_factory() as session:
            rows = await _events_after(session, run_id, last, 200, public=public)
            final: dict[str, Any] | None = None
            if not rows:
                run = await session.get(Run, run_id)
                if run is None:
                    return
                if await _has_done(session, run_id):
                    return
                if run.status in TERMINAL_STATUSES:
                    final = await _synthetic_done(session, run)
        for row in rows:
            event = _event_json(row, public=public)
            yield _frame(event).encode()
            last = row.seq
            if row.kind == "done":
                return
        if final is not None:
            yield _frame(final).encode()
            return
        now = time.monotonic()
        if now - started >= lifetime:
            return
        if now - last_ping >= PING_INTERVAL_S:
            yield b": ping\n\n"
            last_ping = now
        if not rows:
            await asyncio.sleep(POLL_INTERVAL_S)


@bp.post("/runs/<run_id>/approve")
async def approve_run(run_id: str) -> Response | tuple[Response, int]:
    """Approval of an untrusted run (the token-mode stand-in for the Approve run button).

    Not a public route, so the access guard already requires credentials (plus the CSRF token for
    cookie sessions and open mode); the caller must then hold ``write`` on the run's repository
    (:func:`~coverset.web.principal.require_repo`: 404 when they cannot see it). The run is queued
    with the network blocked, the pending "coverset: awaiting approval" status or check is
    resolved, the approval is audited, and the run is handed to the queue.
    """
    if current_principal().is_public:
        raise Unauthorized()
    if not RUN_ID_PARAM.fullmatch(run_id):
        raise ApiError(404, "not_found")
    settings = app_settings()
    database = app_db()
    async with database.session() as session:
        run = await session.get(Run, run_id)
        if run is None:
            raise ApiError(404, "not_found")
        principal = require_repo(run.repo_id, "write")
        if run.status != "awaiting_approval":
            raise ApiError(409, "not_awaiting_approval")
        if current_auth() == "open":
            approver = "local operator"
        elif principal.kind == "admin":
            approver = "coverset admin"
        else:
            approver = principal.label
        status = await approve_awaiting_run(session, settings, run_id, approver, via="admin")
        if status is not None:
            from coverset.web.audit import record_audit

            record_audit(
                session, principal, "approve", target_kind="run", target_id=run_id, repo_id=run.repo_id
            )
            await session.commit()
    if status is None:
        raise ApiError(409, "not_awaiting_approval")
    result = IntakeResult("created", run_id=run_id, followups=(Followup("approved", run_id),))
    current_app.add_background_task(
        apply_forge_followups, database.sessionmaker, forge_for_app(current_app), settings, result
    )
    if status == "queued":
        await enqueue_run(current_app.extensions, run_id)
    if request.mimetype in ("application/x-www-form-urlencoded", "multipart/form-data"):
        return Response(status=303, headers={"Location": f"/runs/{run_id}"})
    return jsonify({"status": status, "run_id": run_id}), 200
