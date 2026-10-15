"""The forge contract: what the orchestrator needs from a code host, independent of GitHub.

A :class:`Forge` verifies and parses webhooks into :class:`ForgeEvent` values, hands out short-lived
mirror credentials, and posts results back (check runs, commit statuses, one sticky PR comment).
Adapters: :mod:`coverset.forge.github` (App and token modes) and :mod:`coverset.forge.fake`.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

ForgeEventKind = Literal[
    "push",
    "pr_opened",
    "pr_sync",
    "pr_reopened",
    "pr_ready",
    "rerun",
    "action_button",
    "install",
    "uninstall",
    "repo_visibility",
    "ignore",
]
"""What a webhook means to coverset (everything else is ``ignore`` with a recorded reason)."""

CheckStatus = Literal["queued", "in_progress", "completed"]
Conclusion = Literal["success", "failure", "neutral", "cancelled", "timed_out", "action_required", "skipped"]
CommitState = Literal["pending", "success", "failure", "error"]
AnnotationLevel = Literal["notice", "warning", "failure"]

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
"""A full lowercase hex commit SHA; every SHA taken from a payload must match it."""

FULL_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
"""``owner/name`` as GitHub allows them; checked before a name is interpolated into an API path."""

MAX_REF_CHARS = 255
"""Longest branch name accepted from a payload (GitHub's own limit is 255 bytes)."""

MAX_ID = 2**63 - 1
"""Largest numeric id accepted from a payload (the database columns are signed 64-bit)."""

_REF_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")

TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
"""``author_association`` values whose non-fork PRs run without approval."""

WRITE_ROLES = frozenset({"admin", "maintain", "write"})
"""Repository roles allowed to approve an untrusted pull request's run."""

MAX_ACTION_LABEL = 20
MAX_ACTION_DESCRIPTION = 40
MAX_ACTION_IDENTIFIER = 20


class ForgeError(Exception):
    """A forge API call failed (or a fake forge was configured to fail)."""


class ForgeUnsupported(ForgeError):
    """The forge (or its current mode) cannot do this, e.g. check runs in token mode."""


class ForgeEventError(ValueError):
    """A verified webhook payload is malformed (missing fields, a bad SHA, a bad repo name)."""


@dataclass(frozen=True, slots=True)
class RepoRef:
    """A repository as the forge identifies it (``external_id`` is the stable numeric id)."""

    external_id: int
    full_name: str
    owner_id: int | None = None
    private: bool = True
    default_branch: str = "main"
    installation_id: int | None = None
    id: int | None = None
    """coverset's own ``repos.id`` when known (set by :func:`repo_ref_from_model`)."""

    @property
    def owner(self) -> str:
        """The owner login (the part of ``full_name`` before the slash)."""
        return self.full_name.partition("/")[0]

    @property
    def name(self) -> str:
        """The repository name (the part of ``full_name`` after the slash)."""
        return self.full_name.partition("/")[2]


@dataclass(frozen=True, slots=True)
class PullRef:
    """The parts of a pull request that drive trust decisions and runs."""

    number: int
    head_sha: str
    base_sha: str
    head_ref: str | None = None
    base_ref: str | None = None
    title: str = ""
    author: str | None = None
    author_association: str = "NONE"
    from_fork: bool = False
    draft: bool = False

    @property
    def trusted(self) -> bool:
        """Not from a fork and authored by an owner, member or collaborator."""
        return not self.from_fork and self.author_association in TRUSTED_ASSOCIATIONS


@dataclass(frozen=True, slots=True)
class ForgeEvent:
    """One parsed webhook delivery, reduced to what :func:`coverset.orchestrator.intake.intake` needs."""

    kind: ForgeEventKind
    delivery_id: str
    event_name: str
    action: str | None = None
    repo: RepoRef | None = None
    installation_id: int | None = None
    pull: PullRef | None = None
    head_sha: str | None = None
    base_sha: str | None = None
    """PR base SHA, or the push's ``before`` SHA (None for a branch creation)."""
    ref: str | None = None
    author_association: str | None = None
    from_fork: bool = False
    draft: bool = False
    sender: str | None = None
    check_run_id: int | None = None
    check_external_id: str | None = None
    action_identifier: str | None = None
    head_commit_message: str | None = None
    repos_added: tuple[RepoRef, ...] = ()
    repos_removed: tuple[RepoRef, ...] = ()
    reason: str | None = None
    """Why an ``ignore`` event was ignored (stored with the delivery)."""
    payload: Mapping[str, Any] | None = field(default=None, repr=False, compare=False)
    """The decoded payload, stored with the delivery until retention nulls it."""


@dataclass(frozen=True, slots=True)
class Annotation:
    """A check-run annotation (GitHub caps a PATCH at 50 of these)."""

    path: str
    start_line: int
    end_line: int
    level: AnnotationLevel
    message: str
    title: str | None = None

    def to_github(self) -> dict[str, Any]:
        """The REST representation."""
        body: dict[str, Any] = {
            "path": self.path,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "annotation_level": self.level,
            "message": self.message[:65_000],
        }
        if self.title:
            body["title"] = self.title[:255]
        return body


@dataclass(frozen=True, slots=True)
class CheckAction:
    """A check-run button; GitHub limits label 20, description 40 and identifier 20 characters."""

    label: str
    description: str
    identifier: str

    def __post_init__(self) -> None:
        if not 0 < len(self.label) <= MAX_ACTION_LABEL:
            raise ValueError(f"check action label must be 1-{MAX_ACTION_LABEL} chars: {self.label!r}")
        if not 0 < len(self.description) <= MAX_ACTION_DESCRIPTION:
            raise ValueError(f"check action description must be 1-{MAX_ACTION_DESCRIPTION} chars")
        if not 0 < len(self.identifier) <= MAX_ACTION_IDENTIFIER:
            raise ValueError(f"check action identifier must be 1-{MAX_ACTION_IDENTIFIER} chars")

    def to_github(self) -> dict[str, str]:
        """The REST representation."""
        return {"label": self.label, "description": self.description, "identifier": self.identifier}


RERUN_FAILED = CheckAction("Re-run failed", "Run the failed tests again", "rerun-failed")
RERUN_ALL = CheckAction("Re-run all", "Run every test again", "rerun-all")
APPROVE_RUN = CheckAction("Approve run", "Run this PR's tests (network off)", "approve-run")
ACTION_IDENTIFIERS = frozenset({RERUN_FAILED.identifier, RERUN_ALL.identifier, APPROVE_RUN.identifier})


@dataclass(frozen=True, slots=True)
class CheckOutput:
    """A check run's ``output``: title, markdown summary, optional text and annotations."""

    title: str
    summary: str
    text: str | None = None
    annotations: tuple[Annotation, ...] = ()


@dataclass(frozen=True, slots=True)
class ForgeCapabilities:
    """What an adapter (in its current mode) can do."""

    name: str
    check_runs: bool
    commit_statuses: bool
    pr_comments: bool
    hide_comments: bool
    mirror_credentials: bool
    check_actions: bool


@dataclass(frozen=True, slots=True)
class MirrorCredentials:
    """A short-lived token for fetching a mirror, handed to git as ``http.extraHeader``.

    The header travels in the git child's environment (:meth:`git_config_env`, git 2.31+), never on
    its command line (visible to every local user through ``ps``), in a URL or in ``.git/config``;
    ``repr`` hides the token. :meth:`git_config_args` is the fallback for older git only.
    """

    token: str = field(repr=False)
    expires_at: datetime | None = None
    username: str = "x-access-token"

    @property
    def http_extra_header(self) -> str:
        """``Authorization: Basic ...`` for ``http.extraHeader``."""
        raw = f"{self.username}:{self.token}".encode()
        return "Authorization: Basic " + base64.b64encode(raw).decode("ascii")

    def git_config_env(self) -> dict[str, str]:
        """``GIT_CONFIG_COUNT``/``GIT_CONFIG_KEY_0``/``GIT_CONFIG_VALUE_0`` for the git child's environment."""
        return {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": self.http_extra_header,
        }

    def git_config_args(self) -> list[str]:
        """``["-c", "http.extraHeader=..."]``: only for git older than 2.31, which ignores
        :meth:`git_config_env` (the header is then visible in ``ps`` while git runs)."""
        return ["-c", f"http.extraHeader={self.http_extra_header}"]


@runtime_checkable
class Forge(Protocol):
    """A code host adapter. All network methods are async; parsing and verification are pure."""

    name: str
    allowed_events: frozenset[str]
    """Webhook event names this forge's route acts on; any other delivery is recorded and ignored
    (202) before its payload is parsed. Each adapter owns its list (GitHub's is
    :data:`coverset.forge.github_events.ALLOWED_EVENTS`)."""

    def verify_webhook(self, headers: Mapping[str, str], raw_body: bytes) -> bool:
        """True when ``raw_body`` carries a valid signature; False when unsigned, bad or no secret."""
        ...

    def parse_event(self, headers: Mapping[str, str], raw_body: bytes) -> ForgeEvent:
        """Parse a verified delivery; raises :class:`ForgeEventError` on a malformed payload."""
        ...

    async def fetch_mirror_credentials(self, installation_id: int | None, repo: RepoRef) -> MirrorCredentials:
        """A short-lived read token for ``repo``'s git mirror."""
        ...

    async def create_check(
        self,
        repo: RepoRef,
        head_sha: str,
        name: str,
        *,
        external_id: str | None,
        details_url: str | None,
        status: CheckStatus = "queued",
        conclusion: Conclusion | None = None,
        output: CheckOutput | None = None,
        actions: Sequence[CheckAction] = (),
    ) -> int:
        """Create a check run on ``head_sha`` and return its id."""
        ...

    async def update_check(
        self,
        repo: RepoRef,
        check_id: int,
        *,
        status: CheckStatus | None = None,
        conclusion: Conclusion | None = None,
        output: CheckOutput | None = None,
        actions: Sequence[CheckAction] | None = None,
        details_url: str | None = None,
    ) -> None:
        """Update a check run (annotations beyond 50 are sent in further PATCHes)."""
        ...

    async def set_commit_status(
        self,
        repo: RepoRef,
        sha: str,
        state: CommitState,
        *,
        context: str,
        description: str,
        target_url: str | None,
    ) -> None:
        """Post a commit status (the token-mode substitute for a check run)."""
        ...

    async def upsert_pr_comment(
        self, repo: RepoRef, pr_number: int, marker: str, body: str, prior_id: int | None
    ) -> int:
        """Edit the sticky comment in place (stored id first, then a marker scan) or create it."""
        ...

    async def hide_comment(self, repo: RepoRef, comment_id: int) -> None:
        """Minimise a comment as outdated."""
        ...

    async def collaborator_permission(self, repo: RepoRef, login: str) -> str:
        """``login``'s role on ``repo``: ``admin``, ``maintain``, ``write``, ``triage``, ``read`` or
        ``none`` (raises :class:`ForgeError` when the forge cannot tell)."""
        ...

    def capabilities(self) -> ForgeCapabilities:
        """What this adapter can do in its current mode."""
        ...


def header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that works for plain dicts and Werkzeug/Quart headers."""
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if key.lower() == lowered:
            return candidate
    return None


def check_full_name(full_name: str) -> str:
    """Return ``full_name`` if it is a plausible ``owner/name``; raise :class:`ForgeEventError`."""
    if not FULL_NAME_RE.fullmatch(full_name) or ".." in full_name:
        raise ForgeEventError(f"implausible repository name {full_name!r}")
    return full_name


def check_sha(value: object, what: str) -> str:
    """Return ``value`` if it is a 40-hex SHA; raise :class:`ForgeEventError` otherwise."""
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise ForgeEventError(f"{what} is not a 40-hex SHA")
    return value


def check_id(value: object, what: str, *, minimum: int = 0) -> int:
    """Return ``value`` if it is an integer in ``[minimum, 2**63 - 1]``; raise :class:`ForgeEventError`.

    Larger values would overflow the signed 64-bit columns (a 500 that GitHub redelivers forever).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ForgeEventError(f"{what} is not an integer")
    if not minimum <= value <= MAX_ID:
        raise ForgeEventError(f"{what} is out of range")
    return value


def check_ref(value: object, what: str) -> str:
    """Return ``value`` if it is a branch name ``git check-ref-format --allow-onelevel`` accepts,
    at most 255 characters and not starting with ``-``; raise :class:`ForgeEventError` otherwise.

    The rules are git's (no ``..``, ``@{``, ``//``, control characters, space, ``~^:?*[\\``; no
    component starting with ``.`` or ending with ``.lock``; not ``@``; no leading or trailing ``/``,
    no trailing ``.``), applied without a subprocess.
    """
    if not isinstance(value, str) or not 0 < len(value) <= MAX_REF_CHARS:
        raise ForgeEventError(f"{what} is not a ref name of 1-{MAX_REF_CHARS} characters")
    if (
        value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or value == "@"
        or _REF_FORBIDDEN_RE.search(value)
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
        or any("\ud800" <= ch <= "\udfff" for ch in value)
    ):
        raise ForgeEventError(f"{what} is not a valid ref name")
    return value


def repo_ref_from_model(repo: Any) -> RepoRef:
    """A :class:`RepoRef` for a :class:`coverset.db.models.Repo` row."""
    return RepoRef(
        external_id=int(repo.external_id),
        full_name=repo.full_name,
        owner_id=repo.owner_id,
        private=repo.private,
        default_branch=repo.default_branch,
        installation_id=repo.installation_id,
        id=repo.id,
    )
