"""Control-plane tables (SQLAlchemy 2.1 declarative, async-ready).

Row counts here stay O(files) + O(tests) per run; per-line coverage and argument shapes live in the
Parquet facts store. String enums are typed with ``Literal`` aliases and stored as ``VARCHAR(32)``;
every datetime is aware UTC (:class:`coverset.db.types.UTCDateTime`); JSON columns are
:data:`JSONDict` or :data:`JSONList` so in-place edits are saved. Every foreign-key column is the
leading column of some index, because SQLite looks children up by it on every parent delete.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from coverset.config import ExecutorKind, SandboxNetwork
from coverset.db.types import UTCDateTime, new_id, str_enum, utc_now

SECRET_COLUMN_LEN = 2048
"""Length of secret-bearing columns (stateless ``ghs_`` installation tokens are ~520 chars)."""

SHA_LEN = 40
HASH_LEN = 64
KEY_LEN = 128
NAME_LEN = 255
PATH_LEN = 1024

ForgeKind = Literal["github", "fake", "local"]
RunStatus = Literal[
    "queued",
    "awaiting_approval",
    "paused",
    "running",
    "completed",
    "cancelled",
    "abandoned",
    "skipped",
    "error",
]
Conclusion = Literal["success", "failure", "neutral", "cancelled", "timed_out"]
RunTrigger = Literal["pull_request", "push", "baseline", "rerun", "manual"]
PrState = Literal["open", "closed", "merged"]
ShardStatus = Literal["pending", "running", "completed", "failed", "cancelled"]
SnapshotStatus = Literal["building", "ready", "failed", "expired"]
TestOutcome = Literal["passed", "failed", "error", "skipped", "xfailed", "xpassed", "not_run"]
FailedPhase = Literal["setup", "call", "teardown"]
DeliveryStatus = Literal["received", "processed", "ignored", "ignored_owner", "error"]
InvariantStatus = Literal["satisfied", "violated", "untested", "stale", "needs_review"]
InvariantTrigger = Literal["run", "scan"]
FindingSource = Literal["fixture", "heuristic", "anthropic"]
FindingVerdict = Literal["covered", "partially_covered", "not_covered", "abstain"]
Confidence = Literal["low", "medium", "high"]

JSONDict = MutableDict.as_mutable(JSON)
"""A JSON object column that tracks in-place changes to its top-level keys (``d[k] = v``).

Changes inside nested values are not tracked: reassign the top-level key instead.
"""

JSONList = MutableList.as_mutable(JSON)
"""A JSON array column that tracks in-place changes (``append``, ``l[i] = v``, ...)."""

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def new_run_id() -> str:
    """Default primary key for :class:`Run` (``run_<ULID>``)."""
    return new_id("run")


class Base(AsyncAttrs, DeclarativeBase):
    """Declarative base with a constraint naming convention (needed by batch migrations)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Repo(Base):
    """A repository under test, identified by its forge's stable id (never its name)."""

    __tablename__ = "repos"
    __table_args__ = (UniqueConstraint("forge", "external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forge: Mapped[ForgeKind] = mapped_column(str_enum(ForgeKind))
    external_id: Mapped[str] = mapped_column(String(KEY_LEN))
    full_name: Mapped[str] = mapped_column(String(NAME_LEN))
    default_branch: Mapped[str] = mapped_column(String(NAME_LEN), default="main")
    private: Mapped[bool] = mapped_column(Boolean, default=True)
    language: Mapped[str] = mapped_column(String(32), default="python")
    installation_id: Mapped[int | None] = mapped_column(BigInteger)
    owner_id: Mapped[int | None] = mapped_column(BigInteger)
    ai_review_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    settings_json: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class Env(Base):
    """A dependency environment (lockfile + Python + plugin wheel), keyed by its content hash."""

    __tablename__ = "envs"

    env_hash: Mapped[str] = mapped_column(String(HASH_LEN), primary_key=True)
    image_id: Mapped[str | None] = mapped_column(String(KEY_LEN))
    python: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class Snapshot(Base):
    """A trusted filesystem snapshot of a default-branch commit, built on top of an env image."""

    __tablename__ = "snapshots"
    __table_args__ = (Index(None, "repo_id", "status", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    env_hash: Mapped[str] = mapped_column(ForeignKey("envs.env_hash"), index=True)
    sha: Mapped[str] = mapped_column(String(SHA_LEN))
    image_id: Mapped[str | None] = mapped_column(String(KEY_LEN))
    status: Mapped[SnapshotStatus] = mapped_column(str_enum(SnapshotStatus), default="building")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Commit(Base):
    """A commit seen in a repo's mirror (metadata only; source stays in git)."""

    __tablename__ = "commits"
    __table_args__ = (UniqueConstraint("repo_id", "sha"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    sha: Mapped[str] = mapped_column(String(SHA_LEN))
    parent_shas: Mapped[list[str]] = mapped_column(JSONList, default=list)
    subject: Mapped[str] = mapped_column(String(PATH_LEN), default="")
    author_name: Mapped[str | None] = mapped_column(String(NAME_LEN))
    committed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class PullRequest(Base):
    """A pull request, with the sticky comment id once posted."""

    __tablename__ = "pull_requests"
    __table_args__ = (UniqueConstraint("repo_id", "number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    head_sha: Mapped[str] = mapped_column(String(SHA_LEN))
    base_sha: Mapped[str] = mapped_column(String(SHA_LEN))
    head_ref: Mapped[str | None] = mapped_column(String(NAME_LEN))
    base_ref: Mapped[str | None] = mapped_column(String(NAME_LEN))
    title: Mapped[str] = mapped_column(String(PATH_LEN), default="")
    author: Mapped[str | None] = mapped_column(String(NAME_LEN))
    """The PR author's login (an untrusted author may not approve their own PR's run)."""
    author_association: Mapped[str] = mapped_column(String(32), default="NONE")
    from_fork: Mapped[bool] = mapped_column(Boolean, default=False)
    draft: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[PrState] = mapped_column(str_enum(PrState), default="open")
    comment_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)


class Run(Base):
    """One CI run of a head commit (a PR head, a default-branch baseline, or a manual run)."""

    __tablename__ = "runs"
    __table_args__ = (
        Index(None, "repo_id", "head_sha", "status"),
        Index(None, "repo_id", "pr_id", "created_at"),
        Index(None, "repo_id", "branch", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_run_id)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    head_sha: Mapped[str] = mapped_column(String(SHA_LEN))
    base_sha: Mapped[str | None] = mapped_column(String(SHA_LEN))
    branch: Mapped[str | None] = mapped_column(String(NAME_LEN))
    pr_id: Mapped[int | None] = mapped_column(ForeignKey("pull_requests.id", ondelete="SET NULL"), index=True)
    trigger: Mapped[RunTrigger] = mapped_column(str_enum(RunTrigger))
    status: Mapped[RunStatus] = mapped_column(str_enum(RunStatus), default="queued")
    claimed_by: Mapped[str | None] = mapped_column(String(KEY_LEN))
    """The process executing the run (``<role>@<host>:<pid>:<nonce>``); None while queued."""
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    """Refreshed every few seconds while the run executes; a stale one marks a dead executor."""
    verdict: Mapped[Conclusion | None] = mapped_column(str_enum(Conclusion))
    reason: Mapped[str | None] = mapped_column(Text)
    trusted: Mapped[bool] = mapped_column(Boolean, default=True)
    network: Mapped[SandboxNetwork | None] = mapped_column(str_enum(SandboxNetwork))
    """Sandbox network override for this run (``blocked`` for untrusted PRs); None = the setting."""
    approval_check_id: Mapped[int | None] = mapped_column(BigInteger)
    """The forge's "coverset: awaiting approval" check run, while an untrusted PR waits."""
    approved_by: Mapped[str | None] = mapped_column(String(NAME_LEN))
    approval_requested_by: Mapped[str | None] = mapped_column(String(NAME_LEN))
    """Who last pressed Approve run; the run stays awaiting until their write access is verified."""
    check_run_id: Mapped[int | None] = mapped_column(BigInteger)
    """The forge's verdict check run for this run (set by :func:`coverset.forge.verdict.post_verdict`)."""
    executor: Mapped[ExecutorKind] = mapped_column(str_enum(ExecutorKind), default="local")
    env_hash: Mapped[str | None] = mapped_column(ForeignKey("envs.env_hash"), index=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("snapshots.id", ondelete="SET NULL"), index=True
    )
    shard_count: Mapped[int] = mapped_column(Integer, default=0)
    scoring_basis_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    coverage_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    cancelled_tests: Mapped[int] = mapped_column(Integer, default=0)
    cost_estimate: Mapped[float] = mapped_column(Float, default=0.0)
    rank_coverset: Mapped[int | None] = mapped_column(Integer)
    rank_file_order: Mapped[int | None] = mapped_column(Integer)
    rank_executed: Mapped[int | None] = mapped_column(Integer)
    """How many tests had reported a result (across all shards) when the first confirmed failure
    did: its position in actual execution order (``rank_coverset`` is its planned position)."""
    t_first_failure_file_order_est: Mapped[float | None] = mapped_column(Float)
    timeline: Mapped[dict[str, str]] = mapped_column(JSONDict, default=dict)
    ci_first_contact_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    extra_env: Mapped[dict[str, str]] = mapped_column(JSONDict, default=dict)
    """Environment variables applied to every exec of this run (sanitised by the executor)."""
    retry_increment_keys: Mapped[list[str]] = mapped_column(JSONList, default=list)
    """``extra_env`` keys whose integer value a retry exec increments by one."""
    summary_json: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    """The final :class:`~coverset.forge.verdict.VerdictSummary` as JSON (what the forge was sent)."""
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class Shard(Base):
    """One worker's slice of a run; ``shard_id`` is the 0-based shard index within the run."""

    __tablename__ = "shards"

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    shard_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[ShardStatus] = mapped_column(str_enum(ShardStatus), default="pending")
    worker_ref: Mapped[str | None] = mapped_column(String(KEY_LEN))
    test_count: Mapped[int] = mapped_column(Integer, default=0)
    predicted_s: Mapped[float | None] = mapped_column(Float)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    bundle_sha256: Mapped[str | None] = mapped_column(String(HASH_LEN))
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class TestCase(Base):
    """A test node id within a repo; ``nodeid_hash`` is a 16-byte digest of the node id."""

    __tablename__ = "tests"
    __table_args__ = (UniqueConstraint("repo_id", "nodeid_hash"),)
    __test__ = False

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    nodeid: Mapped[str] = mapped_column(String(PATH_LEN))
    nodeid_hash: Mapped[bytes] = mapped_column(LargeBinary(16))
    family: Mapped[str] = mapped_column(String(PATH_LEN))
    file_id: Mapped[int | None] = mapped_column(Integer)
    first_seen_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)


class TestResult(Base):
    """The final outcome of one test in one run (retries are in :class:`TestAttempt`)."""

    __tablename__ = "test_results"
    __table_args__ = (Index(None, "test_id", "run_id"),)
    __test__ = False

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), primary_key=True)
    outcome: Mapped[TestOutcome] = mapped_column(str_enum(TestOutcome))
    failed_phase: Mapped[FailedPhase | None] = mapped_column(str_enum(FailedPhase))
    fail_sig: Mapped[bytes | None] = mapped_column(LargeBinary(8))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    shard_id: Mapped[int | None] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    flaky: Mapped[bool] = mapped_column(Boolean, default=False)
    quarantined: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float | None] = mapped_column(Float)
    score_explanation: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    rank: Mapped[int | None] = mapped_column(Integer)


class TestAttempt(Base):
    """One execution of a test within a run (attempt 1 is the shard run, later ones are retries)."""

    __tablename__ = "test_attempts"
    __table_args__ = (Index(None, "test_id"),)
    __test__ = False

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    outcome: Mapped[TestOutcome] = mapped_column(str_enum(TestOutcome))
    duration_ms: Mapped[float | None] = mapped_column(Float)
    shard_id: Mapped[int | None] = mapped_column(Integer)


class TestStats(Base):
    """Rolling per-test statistics recomputed at ingest over the last 50 executed results."""

    __tablename__ = "test_stats"
    __table_args__ = (Index(None, "test_id"),)
    __test__ = False

    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"), primary_key=True)
    test_id: Mapped[int] = mapped_column(ForeignKey("tests.id", ondelete="CASCADE"), primary_key=True)
    runs_n: Mapped[int] = mapped_column(Integer, default=0)
    fails_n: Mapped[int] = mapped_column(Integer, default=0)
    last_fail_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    last_fail_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    fail_ewma: Mapped[float] = mapped_column(Float, default=0.0)
    dur_ewma_ms: Mapped[float | None] = mapped_column(Float)
    p50_ms: Mapped[float | None] = mapped_column(Float)
    p95_ms: Mapped[float | None] = mapped_column(Float)
    flaky_n: Mapped[int] = mapped_column(Integer, default=0)
    flaky_score: Mapped[float] = mapped_column(Float, default=0.0)
    updated_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)


class RunEvent(Base):
    """An ordered, redacted event in a run's stream (served over SSE and JSON polling)."""

    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class WebhookDelivery(Base):
    """A received webhook delivery; ``payload`` is nulled by retention, the row kept for dedupe."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index(None, "received_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(KEY_LEN), unique=True)
    body_sha256: Mapped[str | None] = mapped_column(String(HASH_LEN), unique=True)
    """SHA-256 of the signed raw body: a captured delivery replayed with a fresh GUID is a duplicate."""
    forge: Mapped[ForgeKind] = mapped_column(str_enum(ForgeKind))
    event: Mapped[str] = mapped_column(String(64))
    action: Mapped[str | None] = mapped_column(String(64))
    repo_external_id: Mapped[str | None] = mapped_column(String(KEY_LEN))
    status: Mapped[DeliveryStatus] = mapped_column(str_enum(DeliveryStatus), default="received")
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class ForgeCall(Base):
    """A call made (or, for the fake forge, recorded) against a forge API; feeds ``/forge/outbox``."""

    __tablename__ = "forge_calls"
    __table_args__ = (Index(None, "repo_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forge: Mapped[ForgeKind] = mapped_column(str_enum(ForgeKind))
    repo_id: Mapped[int | None] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    method: Mapped[str] = mapped_column(String(64))
    target: Mapped[str | None] = mapped_column(String(NAME_LEN))
    request: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONDict)
    status_code: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class Invariant(Base):
    """One version of a declared invariant; a changed spec is a new row (new ``spec_hash``)."""

    __tablename__ = "invariants"
    __table_args__ = (UniqueConstraint("repo_id", "inv_key", "spec_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id", ondelete="CASCADE"))
    inv_key: Mapped[str] = mapped_column(String(KEY_LEN))
    spec_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    spec_json: Mapped[dict[str, Any]] = mapped_column(JSONDict)
    first_seen_sha: Mapped[str] = mapped_column(String(SHA_LEN))
    last_seen_sha: Mapped[str] = mapped_column(String(SHA_LEN))


class InvariantResult(Base):
    """The status of one invariant version in one run (or default-branch scan)."""

    __tablename__ = "invariant_results"
    __table_args__ = (Index(None, "invariant_id", "run_id"),)

    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    invariant_id: Mapped[int] = mapped_column(
        ForeignKey("invariants.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[InvariantStatus] = mapped_column(str_enum(InvariantStatus))
    trigger: Mapped[InvariantTrigger] = mapped_column(str_enum(InvariantTrigger), default="run")
    target_func_key: Mapped[str | None] = mapped_column(String(32))
    target_body_hash: Mapped[str | None] = mapped_column(String(HASH_LEN))
    covering_test_ids: Mapped[list[int]] = mapped_column(JSONList, default=list)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)


class ReviewFinding(Base):
    """An advisory AI-review finding for one function body, reusable for an identical bundle hash."""

    __tablename__ = "review_findings"
    __table_args__ = (Index(None, "func_key", "bundle_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    func_key: Mapped[str] = mapped_column(String(32))
    body_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    bundle_hash: Mapped[str] = mapped_column(String(HASH_LEN))
    source: Mapped[FindingSource] = mapped_column(str_enum(FindingSource))
    verdict: Mapped[FindingVerdict] = mapped_column(str_enum(FindingVerdict))
    confidence: Mapped[Confidence] = mapped_column(str_enum(Confidence))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONDict, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)


class AppSetting(Base):
    """Control-plane key/value state (e.g. the Modal instance uuid, setup progress).

    ``value`` is sized for secrets so a stored token is never truncated.
    """

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(KEY_LEN), primary_key=True)
    value: Mapped[str] = mapped_column(String(SECRET_COLUMN_LEN))
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, onupdate=utc_now)
