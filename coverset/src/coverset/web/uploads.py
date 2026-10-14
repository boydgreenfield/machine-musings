"""``POST /v1/bundles``: upload mode's intake, and ``GET /v1/bundles/<run_id>`` to follow the run.

Both are public routes for the access guard (``web/auth.is_public``); these views authorize every
caller themselves, before reading a byte of the body:

* the admin (admin token bearer, or an instance-admin principal the guard resolved);
* a ``cst_`` API token with the ``upload`` scope (:func:`coverset.web.tokens.verify`, checked here
  even when the guard already resolved it, so the rule does not depend on the guard's mode): a
  repository token may upload only for its own repository (403 for another one), a personal token
  for repositories its user may push to (``repo_access`` write or admin);
* a GitHub Actions OIDC token (:func:`coverset.forge.oidc.verify_actions_oidc`, audience
  ``COVERSET_OIDC_AUDIENCE``): its ``repository`` must be the repository named in the request
  (403 otherwise) and its ``repository_id``/``repository_owner_id`` must match the stored row. A
  repository coverset does not know yet is created from the token's claims when its owner is listed
  in ``COVERSET_ALLOWED_OWNERS`` (and passes ``COVERSET_ALLOWED_REPOS``); otherwise 404.

Anything else is 401. Uploads switched off (``COVERSET_UPLOADS_ENABLED=false``) answer 404, a body
over ``COVERSET_MAX_UPLOAD_BYTES`` 413 (checked against ``Content-Length`` first and again while
streaming, since the client sends chunked bodies). Every attempt with a bearer is charged to a
per-address limiter (:data:`UPLOADS_PER_MINUTE`) before this view verifies the credential.

The request names its run in headers (``X-Coverset-Repo``, ``-Sha``, ``-Base``, ``-Branch``
(percent-encoded), ``-Pr``, ``-Pr-Title``, ``-Label``, ``-Shards``, ``-Archive-Sha256``) and the body
is the gzipped tar ``coverset-ci upload`` builds. The body is streamed to a 0600 file under
``<runs_dir>/.uploads``, unpacked under the untrusted-bundle limits, and registered
(:func:`coverset.orchestrator.upload.register_upload`) under a per-commit lock; the answer is 202
``{run_id, url, state, shard, label, duplicate}`` and the run is processed in the background
(:class:`~coverset.orchestrator.upload.UploadWorker`). Every accepted upload is audited
(``bundle_uploaded``).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import shutil
import urllib.parse
import weakref
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from quart import Blueprint, Response, current_app, jsonify, request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import ApiToken, PullRequest, Repo, RepoAccess, Run, RunEvent, Shard, User
from coverset.forge.oidc import Claims, OidcError, verify_actions_oidc
from coverset.orchestrator.intake import owner_allowed
from coverset.orchestrator.runner import OrchestratorContext
from coverset.orchestrator.upload import (
    LABEL_RE,
    Registration,
    UploadError,
    UploadMeta,
    UploadWorker,
    parse_worker_ref,
    register_upload,
    unpack_upload,
)
from coverset.web import tokens
from coverset.web.api import JWK_CLIENT_EXTENSION, OUTCOMES, SlidingWindowLimiter, client_key
from coverset.web.context import app_db, app_settings
from coverset.web.principal import current_principal

bp = Blueprint("uploads", __name__)

UPLOADS_PER_MINUTE = 600
"""Upload and status requests with a non-admin bearer per client address per minute (a matrix of
waiting jobs polls about 20 times a minute each; behind a proxy without COVERSET_TRUST_PROXY they
all share the proxy's address)."""
WORKER_EXTENSION = "upload_worker"
LIMITER_EXTENSION = "upload_limiter"
LOCKS_EXTENSION = "upload_locks"
OIDC_FORGES = ("github", "fake")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_SHARDS = 256
MAX_FAILURES_SHOWN = 20
UPLOAD_SCOPE = "upload"
NO_REPOSITORY = "-/-"
"""A full name no repository can have (GitHub names never start with ``-``): the status route
authorizes a request for an unknown run against it, so the credential check runs either way."""


@dataclass(frozen=True, slots=True)
class Uploader:
    """Who is uploading, once authorized for one repository."""

    repo: Repo
    actor: str
    """``admin``, ``repo token <prefix>``, ``token <prefix> of @login`` or ``oidc <owner/name>``."""
    trusted: bool = True


@bp.errorhandler(UploadError)
async def _upload_error(error: UploadError) -> tuple[Response, int]:
    body: dict[str, Any] = {"error": error.slug}
    if error.detail:
        body["detail"] = error.detail
    response = jsonify(body)
    if error.status == 401:
        response.headers["WWW-Authenticate"] = 'Bearer realm="coverset"'
    if error.status == 429:
        response.headers["Retry-After"] = error.detail or "60"
    return response, error.status


def upload_worker(app: Any) -> UploadWorker:
    """The app's upload worker (created on first use, on the app's orchestrator context)."""
    worker = app.extensions.get(WORKER_EXTENSION)
    if worker is None:
        worker = app.extensions[WORKER_EXTENSION] = UploadWorker(OrchestratorContext.from_app(app))
    return worker


def _limiter() -> SlidingWindowLimiter:
    limiter = current_app.extensions.get(LIMITER_EXTENSION)
    if limiter is None:
        limiter = current_app.extensions[LIMITER_EXTENSION] = SlidingWindowLimiter(UPLOADS_PER_MINUTE)
    return limiter


def _lock(key: tuple[int, str]) -> asyncio.Lock:
    """The registration lock of one commit (dropped once nobody holds a reference)."""
    locks: weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock] = current_app.extensions.setdefault(
        LOCKS_EXTENSION, weakref.WeakValueDictionary()
    )
    lock = locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        locks[key] = lock
    return lock


def _bearer() -> str | None:
    scheme, _, value = request.headers.get("Authorization", "").partition(" ")
    return value.strip() or None if scheme.lower() == "bearer" else None


def _charge() -> None:
    retry_after = _limiter().hit(client_key())
    if retry_after is not None:
        raise UploadError(429, "rate_limited", str(int(retry_after + 0.999)))


# -- authorization --------------------------------------------------------------------------------


async def _repo_by_name(session: AsyncSession, full_name: str) -> Repo | None:
    """The repository called ``full_name`` (case-insensitive; a GitHub row wins over others)."""
    rows = list(await session.scalars(select(Repo).where(func.lower(Repo.full_name) == full_name.lower())))
    order = {"github": 0, "fake": 1, "local": 2}
    rows.sort(key=lambda r: (order.get(r.forge, 9), r.id))
    return rows[0] if rows else None


def _dev_user_refused(user: User) -> bool:
    """True for a development sign-in user while the dev sign-in's conditions do not hold (their
    tokens must not outlast it; see :func:`coverset.web.account.dev_user_refused`)."""
    from coverset.web.account import dev_user_refused

    return dev_user_refused(user)


async def _token_uploader(session: AsyncSession, raw: str, full_name: str) -> Uploader:
    row: ApiToken | None = await tokens.verify(session, raw, now=app_settings().now())
    if row is None:
        raise UploadError(401, "invalid_token")
    scopes = {str(s) for s in (row.scopes or [])}
    if UPLOAD_SCOPE not in scopes:
        raise UploadError(403, "insufficient_scope", "the token needs the upload scope")
    if row.kind == "repo":
        repo = await session.get(Repo, row.repo_id) if row.repo_id is not None else None
        if repo is None:
            raise UploadError(401, "invalid_token")
        if repo.full_name.lower() != full_name.lower():
            raise UploadError(403, "wrong_repository", "this token uploads for another repository")
        return Uploader(repo, f"repo token {row.prefix}")
    user = await session.get(User, row.user_id) if row.user_id is not None else None
    if user is None or user.disabled or _dev_user_refused(user):
        raise UploadError(401, "invalid_token")
    repo = await _repo_by_name(session, full_name)
    if repo is None:
        raise UploadError(404, "unknown_repository")
    if user.role != "admin":
        permission = await session.scalar(
            select(RepoAccess.permission).where(RepoAccess.user_id == user.id, RepoAccess.repo_id == repo.id)
        )
        if permission is None:
            raise UploadError(404, "unknown_repository")
        if permission not in ("write", "admin"):
            raise UploadError(403, "insufficient_permission", "uploading needs write access")
    return Uploader(repo, f"token {row.prefix} of @{user.login}")


async def _verify_oidc(raw: str) -> Claims:
    kwargs: dict[str, Any] = {"audience": app_settings().oidc_audience}
    if (client := current_app.extensions.get(JWK_CLIENT_EXTENSION)) is not None:
        kwargs["jwk_client"] = client
    try:
        return await asyncio.to_thread(verify_actions_oidc, raw, **kwargs)
    except OidcError:
        raise UploadError(401, "invalid_token") from None


async def _oidc_uploader(session: AsyncSession, raw: str, full_name: str, *, create: bool) -> Uploader:
    claims = await _verify_oidc(raw)
    if not claims.repository or claims.repository.lower() != full_name.lower():
        raise UploadError(403, "wrong_repository", "the OIDC token was issued to another repository")
    repo = await session.scalar(
        select(Repo)
        .where(Repo.external_id == str(claims.repository_id), Repo.forge.in_(OIDC_FORGES))
        .order_by(Repo.forge.desc())
        .limit(1)
    )
    if repo is not None and repo.owner_id is not None and repo.owner_id != claims.repository_owner_id:
        raise UploadError(403, "wrong_repository", "the token's owner does not match the repository")
    if repo is None:
        repo = await _create_from_claims(session, claims) if create else None
        if repo is None:
            raise UploadError(
                404,
                "unknown_repository",
                "coverset does not know this repository: install the GitHub App, or list its owner "
                "in COVERSET_ALLOWED_OWNERS",
            )
    trusted = claims.event_name != "pull_request_target"
    return Uploader(repo, f"oidc {claims.repository}", trusted=trusted)


async def _create_from_claims(session: AsyncSession, claims: Claims) -> Repo | None:
    """A ``repos`` row from a verified token, when ``COVERSET_ALLOWED_OWNERS`` lists its owner."""
    settings = app_settings()
    owner = claims.raw.get("repository_owner")
    if not settings.allowed_owners or not isinstance(owner, str) or not claims.repository:
        return None
    if claims.repository.partition("/")[0].lower() != owner.lower():
        return None
    if not owner_allowed(settings, claims.repository):
        return None
    visibility = claims.raw.get("repository_visibility")
    repo = Repo(
        forge="github",
        external_id=str(claims.repository_id),
        full_name=claims.repository,
        owner_id=claims.repository_owner_id,
        private=visibility != "public",
        default_branch="main",
    )
    session.add(repo)
    await session.flush()
    return repo


async def authorize_upload(session: AsyncSession, full_name: str, *, create: bool = True) -> Uploader:
    """The uploader for repository ``full_name``, or :class:`UploadError` (see the module doc)."""
    principal = current_principal()
    bearer = _bearer()
    if principal.instance_admin and (bearer is None or not tokens.looks_like_token(bearer)):
        repo = await _repo_by_name(session, full_name)
        if repo is None:
            raise UploadError(404, "unknown_repository")
        return Uploader(repo, principal.label or "admin")
    if bearer is None:
        raise UploadError(401, "unauthorized")
    _charge()
    if tokens.looks_like_token(bearer):
        return await _token_uploader(session, bearer, full_name)
    return await _oidc_uploader(session, bearer, full_name, create=create)


# -- POST /v1/bundles -----------------------------------------------------------------------------


def _header(name: str) -> str | None:
    value = request.headers.get(name, "").strip()
    return value or None


def _int_header(name: str, low: int, high: int) -> int | None:
    value = _header(name)
    if value is None:
        return None
    if not value.isascii() or not value.isdigit() or not low <= int(value) <= high:
        raise UploadError(400, "bad_request", f"{name} must be an integer from {low} to {high}")
    return int(value)


def _text_header(name: str, limit: int) -> str | None:
    """A percent-encoded header without control characters, capped at ``limit`` characters."""
    value = _header(name)
    if value is None:
        return None
    decoded = urllib.parse.unquote(value)
    if len(decoded) > limit or any(ord(c) < 0x20 or c == "\x7f" for c in decoded):
        raise UploadError(400, "bad_request", f"{name} is not usable")
    return decoded


@dataclass(frozen=True, slots=True)
class _Request:
    repo: str
    sha: str
    base: str | None
    branch: str | None
    pr: int | None
    pr_title: str | None
    label: str
    shards: int | None
    digest: str | None


def _parse_headers() -> _Request:
    repo = _header("X-Coverset-Repo") or ""
    sha = (_header("X-Coverset-Sha") or "").lower()
    base = (_header("X-Coverset-Base") or "").lower() or None
    label = _header("X-Coverset-Label") or "default"
    digest = (_header("X-Coverset-Archive-Sha256") or "").lower() or None
    if not REPO_RE.fullmatch(repo):
        raise UploadError(400, "bad_request", "X-Coverset-Repo must be OWNER/NAME")
    if not SHA_RE.fullmatch(sha):
        raise UploadError(400, "bad_request", "X-Coverset-Sha must be a 40-hex commit SHA")
    if base is not None and not SHA_RE.fullmatch(base):
        raise UploadError(400, "bad_request", "X-Coverset-Base must be a 40-hex commit SHA")
    if not LABEL_RE.fullmatch(label):
        raise UploadError(400, "bad_request", "X-Coverset-Label: 1-64 of A-Z a-z 0-9 . _ -")
    if digest is not None and not DIGEST_RE.fullmatch(digest):
        raise UploadError(400, "bad_request", "X-Coverset-Archive-Sha256 must be 64 hex characters")
    return _Request(
        repo=repo,
        sha=sha,
        base=base if base != sha else None,
        branch=_text_header("X-Coverset-Branch", 255),
        pr=_int_header("X-Coverset-Pr", 1, 2**31 - 1),
        pr_title=_text_header("X-Coverset-Pr-Title", 255),
        label=label,
        shards=_int_header("X-Coverset-Shards", 1, MAX_SHARDS),
        digest=digest,
    )


async def _receive(target: Path, limit: int) -> tuple[str, int]:
    """Stream the request body into ``target`` (0600); ``(sha256, bytes)``; 413 past ``limit``."""
    digest = hashlib.sha256()
    size = 0
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out:
        async for chunk in request.body:
            size += len(chunk)
            if size > limit:
                raise UploadError(413, "too_large", f"the upload exceeds {limit} bytes")
            digest.update(chunk)
            out.write(chunk)
    return digest.hexdigest(), size


@bp.post("/v1/bundles")
async def upload_bundle() -> tuple[Response, int]:
    """Accept one shard's upload archive (see the module docstring); 202 with the run."""
    settings = app_settings()
    if not settings.uploads_enabled:
        raise UploadError(404, "not_found")
    meta_in = _parse_headers()
    db = app_db()
    async with db.session() as session:
        uploader = await authorize_upload(session, meta_in.repo)
        await session.commit()
        repo_id = uploader.repo.id
    length = request.content_length
    if length is not None and length > settings.max_upload_bytes:
        raise UploadError(413, "too_large", f"the upload exceeds {settings.max_upload_bytes} bytes")
    incoming = settings.runs_dir / ".uploads"
    incoming.mkdir(mode=0o700, parents=True, exist_ok=True)
    token = secrets.token_hex(8)
    archive_path = incoming / f"{token}.tar.gz"
    unpack_dir = incoming / f"{token}.d"
    try:
        digest, size = await _receive(archive_path, settings.max_upload_bytes)
        if size == 0:
            raise UploadError(400, "empty_upload", "the request has no body")
        if meta_in.digest is not None and meta_in.digest != digest:
            raise UploadError(400, "digest_mismatch", "the body does not match X-Coverset-Archive-Sha256")
        unpacked = await asyncio.to_thread(unpack_upload, archive_path, unpack_dir, settings)
        meta = UploadMeta(
            repo_id=repo_id,
            head_sha=meta_in.sha,
            base_sha=meta_in.base,
            branch=meta_in.branch,
            pr_number=meta_in.pr,
            label=meta_in.label,
            archive_sha256=digest,
            archive_bytes=size,
            expected_shards=meta_in.shards,
            pr_title=meta_in.pr_title,
            trusted=uploader.trusted,
            actor=uploader.actor,
        )
        worker = upload_worker(current_app)
        async with _lock((repo_id, meta_in.sha)):
            registration = await register_upload(db, settings, meta, unpacked, claimant=worker.ctx.claimant)
    finally:
        await asyncio.to_thread(_cleanup, archive_path, unpack_dir)
    if not registration.duplicate or registration.state == "running":
        worker.schedule(registration.run_id)
    await _audit(uploader, registration, meta)
    return jsonify(await _answer(registration, meta_in)), 202


def _cleanup(*paths: Path) -> None:
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


async def _answer(registration: Registration, meta_in: _Request) -> dict[str, Any]:
    public = app_settings().public_url
    body: dict[str, Any] = {
        "run_id": registration.run_id,
        "url": f"{public}/runs/{registration.run_id}",
        "state": registration.state,
        "shard": registration.shard_id,
        "label": meta_in.label,
        "duplicate": registration.duplicate,
    }
    async with app_db().session() as session:
        run = await session.get(Run, registration.run_id)
        if run is not None and run.pr_id is not None:
            pr = await session.get(PullRequest, run.pr_id)
            if pr is not None:
                body["pr_url"] = f"{public}/repos/{run.repo_id}/prs/{pr.number}"
    return body


async def _audit(uploader: Uploader, registration: Registration, meta: UploadMeta) -> None:
    from coverset.web.audit import audit_now

    await audit_now(
        "bundle_uploaded",
        target_kind="run",
        target_id=registration.run_id,
        repo_id=uploader.repo.id,
        details={
            "uploader": uploader.actor,
            "label": meta.label,
            "sha": meta.head_sha,
            "bytes": meta.archive_bytes,
            "sha256": meta.archive_sha256,
            "duplicate": registration.duplicate,
        },
    )


# -- GET /v1/bundles/<run_id> ---------------------------------------------------------------------


@bp.get("/v1/bundles/<run_id>")
async def upload_status(run_id: str) -> Response:
    """The state of an upload run for the uploader that sent it (``coverset-ci upload --wait``).

    ``{run_id, state, conclusion, exit_code, counts, summary, summary_url, patch_pct, failures,
    shards}``; ``exit_code`` is the last ``done`` event's (null while running).
    """
    settings = app_settings()
    if not settings.uploads_enabled:
        raise UploadError(404, "not_found")
    if not RUN_ID_RE.fullmatch(run_id):
        raise UploadError(404, "not_found")
    async with app_db().session() as session:
        run = await session.get(Run, run_id)
        repo = await session.get(Repo, run.repo_id) if run is not None else None
        known = run is not None and repo is not None and run.trigger == "upload"
        # The credential is resolved the same way whether or not the run exists: an unknown run is
        # authorized against a repository that cannot exist, so a bad bearer is 401 and a valid one
        # that does not own the run is 404 in both cases (no existence oracle).
        try:
            await authorize_upload(session, repo.full_name if known else NO_REPOSITORY, create=False)
        except UploadError as exc:
            if exc.status in (403, 404):
                raise UploadError(404, "not_found") from None
            raise
        if not known:
            raise UploadError(404, "not_found")
        assert run is not None
        return jsonify(await _status(session, run))


async def _status(session: AsyncSession, run: Run) -> dict[str, Any]:
    rows = await session.execute(
        select(RunEvent.payload)
        .where(RunEvent.run_id == run.id, RunEvent.kind == "done")
        .order_by(RunEvent.seq.desc())
        .limit(1)
    )
    done = rows.scalar_one_or_none() or {}
    from coverset.web.api import run_counts

    counts = await run_counts(session, run.id)
    summary = run.summary_json or {}
    shards = []
    for shard in await session.scalars(select(Shard).where(Shard.run_id == run.id).order_by(Shard.shard_id)):
        parsed = parse_worker_ref(shard.worker_ref)
        shards.append(
            {"shard": shard.shard_id, "label": parsed[1] if parsed else None, "status": shard.status}
        )
    finished = run.status in ("completed", "error", "cancelled", "abandoned", "skipped")
    failures = [
        {"nodeid": f.get("nodeid"), "message": f.get("message")}
        for f in (summary.get("failures") or [])[:MAX_FAILURES_SHOWN]
        if isinstance(f, dict)
    ]
    return {
        "run_id": run.id,
        "state": "completed" if finished else "running",
        "conclusion": run.verdict if finished else None,
        "exit_code": done.get("exit_code") if finished else None,
        "counts": {k: counts.get(k, 0) for k in OUTCOMES},
        "summary": run.reason,
        "summary_url": f"{app_settings().public_url}/runs/{run.id}",
        "patch_pct": summary.get("patch_pct"),
        "failures": failures,
        "shards": shards,
    }
