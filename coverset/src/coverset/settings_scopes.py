"""Scoped settings: one catalogue of keys, four scopes, and the inheritance between them.

A setting is a ``scoped_settings`` row ``(scope_kind, scope_id, key) -> {"v": value}``. Scopes:

* ``instance`` (``scope_id = ""``): the whole control plane;
* ``org``: every repository whose ``full_name`` starts with ``<owner>/`` (the id is the owner,
  lower-cased, because GitHub owner names are case-insensitive);
* ``repo``: one repository (the id is ``repos.id`` as a string, never its name, so a rename keeps
  its settings);
* ``user``: one person's view preferences (``users.id`` as a string, or ``"local"`` for the
  operator of an open-mode or admin-token instance, which has no ``users`` row).

Two groups resolve differently. **View preferences** (``view.*``) are about how the UI paints
coverage and belong to the reader: ``user > repo > org > instance > built-in default``. **Policy**
(``policy.*``) decides what the orchestrator does with a pull request and belongs to whoever
answers for the repository's CI: ``repo > org > instance > COVERSET_* configuration > built-in
default``; a user can never override it. Three policy keys (``fail_fast``, ``max_shards``,
``target_shard_seconds``) have no instance configuration: unset everywhere, the repository's own
``[tool.coverset]`` decides, and ``max_shards`` is always clamped by ``COVERSET_MAX_SHARDS``.

The legacy per-repository ``repos.ai_review_enabled`` column is folded into the chain of
``policy.ai_review`` as an implicit repository-level ``true``: an explicit repository row wins over
it, and it wins over the organization and instance values (it was always a per-repository opt-in).

Who may edit (enforced by the pages and the CLI, described by :attr:`SettingSpec.repo_edit`):
instance and org scopes need an instance admin; the repo scope needs write access to the
repository (``admin`` for the keys that change cost or exposure); the user scope is the user's own.

Reads go through a small per-process cache keyed by ``(engine, scope_kind, scope_id)``: each scope's
rows are loaded once and kept for :data:`CACHE_TTL_S` seconds, or until :func:`set_value` /
:func:`clear_value` in this process invalidates them. Another process (the CLI, a second ``serve``)
sees a write within the TTL. Values are re-validated on read, so a row the catalogue no longer
accepts falls through to the next scope instead of breaking a page or a run.
"""

from __future__ import annotations

import dataclasses
import logging
import math
import re
import time
import weakref
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, cast

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.db.models import ActorKind, AuditEvent, Repo, ScopedSetting, SettingScope
from coverset.db.types import utc_now

if TYPE_CHECKING:
    from coverset.config import Settings, UntrustedPrPolicy

log = logging.getLogger(__name__)

ScopeKind = SettingScope
Group = Literal["view", "policy"]
ValueKind = Literal["enum", "bool", "int", "float"]
RepoEdit = Literal["write", "admin"]
FailFast = Literal["finish_in_background", "cancel_remaining"]
Source = Literal["user", "repo", "repo-column", "org", "instance", "config", "deferred", "default"]
"""Where a resolved value came from: a scope row, the legacy repo column, ``COVERSET_*``
configuration, the repository's ``[tool.coverset]`` (``deferred``: the value is None), or the
catalogue default."""

LOCAL_USER: Final = "local"
"""User-scope id of the operator when there is no ``users`` row (open mode, the admin token)."""
CACHE_TTL_S: Final = 30.0
"""How long one process trusts a scope's cached rows (other processes' writes show up within it)."""
SCOPES: Final[tuple[ScopeKind, ...]] = ("instance", "org", "repo", "user")
VIEW_CHAIN: Final[tuple[ScopeKind, ...]] = ("user", "repo", "org", "instance")
POLICY_CHAIN: Final[tuple[ScopeKind, ...]] = ("repo", "org", "instance")
SCOPE_LABELS: Final[Mapping[str, str]] = {
    "user": "user preference",
    "repo": "repository",
    "repo-column": "repository opt-in (repos.ai_review_enabled)",
    "org": "organization",
    "instance": "instance",
    "config": "configuration",
    "deferred": "[tool.coverset]",
    "default": "built-in default",
}
_ORG_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")
_TRUE = frozenset({"true", "1", "yes", "on"})
_FALSE = frozenset({"false", "0", "no", "off"})


class RepoLike(Protocol):
    """What resolution needs of a repository (a ``Repo`` row, or a cached stand-in)."""

    @property
    def id(self) -> int: ...

    @property
    def full_name(self) -> str: ...

    @property
    def ai_review_enabled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class RepoStub:
    """A detached :class:`RepoLike` (the UI's per-request lookups cache these, not ORM rows)."""

    id: int
    full_name: str
    ai_review_enabled: bool = False
    created_at: datetime | None = None
    """When the row was created: repo-scope rows older than this belong to a purged predecessor."""


class SettingError(ValueError):
    """A key, scope or value the catalogue does not accept (the message is safe to show)."""


@dataclass(frozen=True, slots=True)
class SettingSpec:
    """One catalogue entry: the key's type, default, where it may be set and who may set it."""

    name: str
    group: Group
    kind: ValueKind
    default: Any
    """The built-in default; None means "the repository's ``[tool.coverset]`` decides"."""
    label: str
    description: str
    scopes: frozenset[ScopeKind]
    choices: tuple[str, ...] = ()
    choice_labels: Mapping[str, str] = field(default_factory=dict)
    minimum: float | None = None
    maximum: float | None = None
    repo_edit: RepoEdit = "write"
    """Repository permission needed to set the key at repo scope (``admin`` for cost and exposure)."""
    config_env: str | None = None
    """The ``COVERSET_*`` variable that supplies the instance fallback, if any."""
    deferred_to: str | None = None
    """For keys whose default is None: what decides when nothing is set (shown in the UI)."""

    @property
    def user_overridable(self) -> bool:
        """True when a user may set the key for themselves (view preferences only)."""
        return "user" in self.scopes

    @property
    def chain(self) -> tuple[ScopeKind, ...]:
        """Scopes in precedence order, narrowest first."""
        return VIEW_CHAIN if self.group == "view" else POLICY_CHAIN

    def parse(self, raw: object) -> Any:
        """Validate ``raw`` (a JSON value or form/CLI text) and return the typed value.

        Text is accepted for every kind so the pages and the CLI share one parser; booleans accept
        exactly ``true/false/1/0/yes/no/on/off`` (any case) and nothing else.
        """
        if self.kind == "enum":
            text = raw.strip() if isinstance(raw, str) else raw
            if not isinstance(text, str) or text not in self.choices:
                raise SettingError(f"{self.name} must be one of {', '.join(self.choices)}")
            return text
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str) and raw.strip().lower() in _TRUE | _FALSE:
                return raw.strip().lower() in _TRUE
            raise SettingError(f"{self.name} must be true or false")
        if self.kind == "int":
            value = _parse_int(raw)
            if value is None:
                raise SettingError(f"{self.name} must be a whole number")
        else:
            value = _parse_float(raw)
            if value is None:
                raise SettingError(f"{self.name} must be a number")
        if (self.minimum is not None and value < self.minimum) or (
            self.maximum is not None and value > self.maximum
        ):
            raise SettingError(f"{self.name} must be between {_num(self.minimum)} and {_num(self.maximum)}")
        return value

    def format(self, value: Any) -> str:
        """A short display form of a value of this key (``on``/``off`` for booleans)."""
        if value is None:
            return "[tool.coverset]" if self.deferred_to else "unset"
        if self.kind == "bool":
            return "on" if value else "off"
        if self.kind == "enum":
            return self.choice_labels.get(value, value)
        return _num(value)

    def editable_at(self, scope: ScopeKind) -> bool:
        return scope in self.scopes


def _parse_int(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        digits = text[1:] if text[:1] in "+-" else text
        if digits.isascii() and digits.isdigit() and len(digits) <= 9:
            return int(text)
    return None


def _parse_float(raw: object) -> float | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str) and raw.strip().isascii() and len(raw.strip()) <= 24:
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if math.isfinite(value) else None


def _num(value: float | None) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return f"{value:g}"


_VIEW_SCOPES: Final = frozenset[ScopeKind]({"instance", "org", "repo", "user"})
_POLICY_SCOPES: Final = frozenset[ScopeKind]({"instance", "org", "repo"})

CATALOGUE: Final[Mapping[str, SettingSpec]] = {
    spec.name: spec
    for spec in (
        SettingSpec(
            name="view.highlight",
            group="view",
            kind="enum",
            default="all",
            label="Source highlighting",
            description="Paint every measured line, or only the lines that never ran or took one branch.",
            scopes=_VIEW_SCOPES,
            choices=("all", "missing"),
            choice_labels={"all": "every measured line", "missing": "only missing coverage"},
        ),
        SettingSpec(
            name="view.map_mode",
            group="view",
            kind="enum",
            default="gap",
            label="Coverage map colour",
            description="What the coverage map colours by default: the input-class gap, line "
            "coverage, or input-class coverage.",
            scopes=_VIEW_SCOPES,
            choices=("gap", "lines", "inputs"),
            choice_labels={"gap": "input-class gap", "lines": "line coverage", "inputs": "input classes"},
        ),
        SettingSpec(
            name="view.map_module_weight",
            group="view",
            kind="enum",
            default="thin",
            label="Module-level code in the map",
            description="How much room module-level code (imports, constants) takes in the coverage "
            "map: full size, a thin strip, or hidden.",
            scopes=_VIEW_SCOPES,
            choices=("full", "thin", "hidden"),
            choice_labels={"full": "full size", "thin": "thin strip", "hidden": "hidden"},
        ),
        SettingSpec(
            name="view.theme",
            group="view",
            kind="enum",
            default="auto",
            label="Theme",
            description="Light, dark, or follow the operating system.",
            scopes=_VIEW_SCOPES,
            choices=("auto", "light", "dark"),
            choice_labels={"auto": "follow the system", "light": "light", "dark": "dark"},
        ),
        SettingSpec(
            name="policy.untrusted_pr_policy",
            group="policy",
            kind="enum",
            default="approve",
            label="Untrusted pull requests",
            description="Pull requests from forks or non-members: wait for a writer to approve, run "
            "with the network blocked, or skip them.",
            scopes=_POLICY_SCOPES,
            choices=("approve", "run_blocked_network", "skip"),
            choice_labels={
                "approve": "wait for approval",
                "run_blocked_network": "run with the network blocked",
                "skip": "skip",
            },
            repo_edit="admin",
            config_env="COVERSET_UNTRUSTED_PR_POLICY",
        ),
        SettingSpec(
            name="policy.run_drafts",
            group="policy",
            kind="bool",
            default=False,
            label="Run draft pull requests",
            description="Start runs for draft pull requests too.",
            scopes=_POLICY_SCOPES,
            config_env="COVERSET_RUN_DRAFTS",
        ),
        SettingSpec(
            name="policy.ai_review",
            group="policy",
            kind="bool",
            default=False,
            label="AI review",
            description="Ask the advisory AI reviewer about changed functions (needs COVERSET_AI and, "
            "for the live reviewer, ANTHROPIC_API_KEY). Sends changed code to Anthropic at the "
            "instance's expense.",
            scopes=_POLICY_SCOPES,
            repo_edit="admin",
        ),
        SettingSpec(
            name="policy.fail_fast",
            group="policy",
            kind="enum",
            default=None,
            label="After the first confirmed failure",
            description="Keep running the remaining tests in the background, or cancel them.",
            scopes=_POLICY_SCOPES,
            choices=("finish_in_background", "cancel_remaining"),
            choice_labels={
                "finish_in_background": "finish in the background",
                "cancel_remaining": "cancel the rest",
            },
            deferred_to="[tool.coverset] fail_fast (default finish_in_background)",
        ),
        SettingSpec(
            name="policy.max_shards",
            group="policy",
            kind="int",
            default=None,
            label="Most shards per run",
            description="Upper bound on parallel workers for one run: caps [tool.coverset] max_shards "
            "(never raises it) and is always capped by COVERSET_MAX_SHARDS.",
            scopes=_POLICY_SCOPES,
            minimum=1,
            maximum=128,
            repo_edit="admin",
            deferred_to="[tool.coverset] max_shards (default 16)",
        ),
        SettingSpec(
            name="policy.target_shard_seconds",
            group="policy",
            kind="int",
            default=None,
            label="Target seconds per shard",
            description="How long each shard should take; smaller values ask for more shards.",
            scopes=_POLICY_SCOPES,
            minimum=1,
            maximum=3600,
            repo_edit="admin",
            deferred_to="[tool.coverset] target_shard_seconds (default 20)",
        ),
        SettingSpec(
            name="policy.test_tax_threshold",
            group="policy",
            kind="float",
            default=8.0,
            label="Test bloat threshold",
            description="Added test statements per newly covered production line above which the "
            "PR verdict flags test bloat (the stored test-economy flag, the PR card and the comment).",
            scopes=_POLICY_SCOPES,
            minimum=0.5,
            maximum=1000,
        ),
        SettingSpec(
            name="policy.public_report",
            group="policy",
            kind="bool",
            default=False,
            label="Public pages",
            description="Let anyone read a public repository's pages without signing in (private "
            "repositories are never public). Not yet enforced: the access layer does not read it yet.",
            scopes=_POLICY_SCOPES,
            repo_edit="admin",
        ),
    )
}
"""Every key coverset knows, in display order. Other modules use these exact names."""

VIEW_KEYS: Final = tuple(name for name, spec in CATALOGUE.items() if spec.group == "view")
POLICY_KEYS: Final = tuple(name for name, spec in CATALOGUE.items() if spec.group == "policy")


def spec_for(key: str) -> SettingSpec:
    """The catalogue entry for ``key`` (:class:`SettingError` for an unknown key)."""
    spec = CATALOGUE.get(key)
    if spec is None:
        raise SettingError(f"unknown setting {key!r}")
    return spec


def keys_for_scope(scope: ScopeKind) -> list[SettingSpec]:
    """The keys that may be set at ``scope``, in catalogue order."""
    return [spec for spec in CATALOGUE.values() if scope in spec.scopes]


# -- scope ids ------------------------------------------------------------------------------------


def org_of(full_name: str) -> str | None:
    """The org-scope id of a repository name (its owner, lower-cased), or None without an owner."""
    owner, sep, _ = full_name.partition("/")
    return owner.lower() if sep and owner else None


def normalize_scope_id(scope: ScopeKind, scope_id: str | int | None) -> str:
    """The stored id for a scope (raises :class:`SettingError` on a malformed one)."""
    if scope == "instance":
        return ""
    text = "" if scope_id is None else str(scope_id).strip()
    if scope == "org":
        text = text.lower()
        if not _ORG_ID.match(text):
            raise SettingError(f"not an organization name: {scope_id!r}")
        return text
    if scope == "repo":
        if not (text.isascii() and text.isdigit()):
            raise SettingError(f"repository scope ids are repository ids, got {scope_id!r}")
        return str(int(text))
    if scope == "user":
        if text == LOCAL_USER or (text.isascii() and text.isdigit()):
            return text
        raise SettingError(f"user scope ids are user ids or {LOCAL_USER!r}, got {scope_id!r}")
    raise SettingError(f"unknown scope {scope!r}")


# -- the per-process cache ------------------------------------------------------------------------

type ScopeKey = tuple[ScopeKind, str]
type ScopeValues = dict[str, Any]
type _ScopeRows = dict[str, tuple[Any, datetime | None]]
"""What the cache keeps per scope: ``key -> (value, updated_at)`` (the stamp lets a reader drop rows
older than the repository they are resolved for; see :func:`load_scopes`)."""


class _TTLCache[K, V]:
    """Values per database engine, each trusted for :data:`CACHE_TTL_S` seconds.

    Keyed weakly by the engine, so tests (and a re-created app) never see another database's rows.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._data: weakref.WeakKeyDictionary[Engine, dict[K, tuple[float, V]]] = weakref.WeakKeyDictionary()

    def get(self, engine: Engine | None, key: K) -> V | None:
        if engine is None:
            return None
        entry = self._data.get(engine, {}).get(key)
        if entry is None or self._clock() - entry[0] > CACHE_TTL_S:
            return None
        return entry[1]

    def put(self, engine: Engine | None, key: K, value: V) -> None:
        if engine is not None:
            self._data.setdefault(engine, {})[key] = (self._clock(), value)

    def invalidate(self, engine: Engine | None, key: K) -> None:
        if engine is not None:
            self._data.get(engine, {}).pop(key, None)

    def clear(self) -> None:
        self._data.clear()


_CACHE: _TTLCache[ScopeKey, _ScopeRows] = _TTLCache()
_REPOS: _TTLCache[int, RepoStub] = _TTLCache()


def clear_cache() -> None:
    """Forget every cached scope (tests; a process that changed rows behind this module's back)."""
    _CACHE.clear()
    _REPOS.clear()


def _engine(session: AsyncSession) -> Engine | None:
    bind = session.bind
    return getattr(bind, "sync_engine", None) if bind is not None else None


async def load_scopes(
    session: AsyncSession,
    scopes: Iterable[ScopeKey],
    *,
    not_before: Mapping[ScopeKey, datetime | None] | None = None,
) -> dict[ScopeKey, ScopeValues]:
    """The valid values stored at each scope (cached; one query for all the uncached scopes).

    ``not_before`` drops a scope's rows last written before the given instant. :func:`load_layers`
    passes the repository's ``created_at`` for its repo scope: repo scope is keyed by ``repos.id``
    with no foreign key, and SQLite reuses the highest rowid, so rows a purged repository left
    behind (when nobody called :func:`forget_scope`) would otherwise be inherited by the next
    repository registered, silently relaxing its untrusted-PR policy. The filter is applied after
    the cache, so a stale cache entry from before a purge in another process is filtered too.
    """
    engine = _engine(session)
    wanted = list(dict.fromkeys(scopes))
    found: dict[ScopeKey, _ScopeRows] = {}
    missing: list[ScopeKey] = []
    for key in wanted:
        cached = _CACHE.get(engine, key)
        if cached is None:
            missing.append(key)
        else:
            found[key] = cached
    if missing:
        fresh: dict[ScopeKey, _ScopeRows] = {key: {} for key in missing}
        condition = or_(
            *(and_(ScopedSetting.scope_kind == kind, ScopedSetting.scope_id == sid) for kind, sid in missing)
        )
        rows = await session.execute(
            select(
                ScopedSetting.scope_kind,
                ScopedSetting.scope_id,
                ScopedSetting.key,
                ScopedSetting.value,
                ScopedSetting.updated_at,
            ).where(condition)
        )
        for kind, sid, key, stored, updated_at in rows:
            value = _stored_value(key, stored, kind)
            if value is not _UNSET:
                fresh[(kind, sid)][key] = (value, updated_at)
        for key, values in fresh.items():
            _CACHE.put(engine, key, values)
        found.update(fresh)
    limits = not_before or {}
    return {key: _rows_since(rows, limits.get(key)) for key, rows in found.items()}


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _rows_since(rows: _ScopeRows, since: datetime | None) -> ScopeValues:
    """The values of ``rows``, without those written before ``since`` (all of them for None)."""
    if since is None:
        return {key: value for key, (value, _) in rows.items()}
    limit = _aware(since)
    kept: ScopeValues = {}
    for key, (value, updated_at) in rows.items():
        if updated_at is not None and _aware(updated_at) < limit:
            log.warning("ignoring %s: written before its repository was created (orphaned row)", key)
            continue
        kept[key] = value
    return kept


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


_UNSET: Final = _Unset()


def _stored_value(key: str, stored: object, scope: str) -> Any:
    """The validated value of a stored row, or ``_UNSET`` (unknown key, wrong scope, bad value)."""
    spec = CATALOGUE.get(key)
    if spec is None or scope not in spec.scopes or not isinstance(stored, dict) or "v" not in stored:
        return _UNSET
    try:
        return spec.parse(stored["v"])
    except SettingError:
        log.warning("ignoring invalid stored setting %s at %s scope: %r", key, scope, stored.get("v"))
        return _UNSET


# -- resolution -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Resolved:
    """A key's effective value and where it came from."""

    key: str
    value: Any
    source: Source
    detail: str
    """Human description of the source (``organization acme``, ``COVERSET_RUN_DRAFTS``...)."""

    @property
    def display(self) -> str:
        return CATALOGUE[self.key].format(self.value)


@dataclass(frozen=True, slots=True)
class Layers:
    """The rows of every scope in play for one request or run, ready to resolve any key."""

    ids: Mapping[ScopeKind, str]
    """Scope ids in play (``instance`` always; ``org``/``repo``/``user`` when known)."""
    values: Mapping[ScopeKind, Mapping[str, Any]]
    settings: Settings
    repo_ai_review_column: bool = False
    org_name: str | None = None

    def at(self, scope: ScopeKind, key: str) -> Any:
        """The value set exactly at ``scope`` (``None`` if the key is not set there)."""
        return self.values.get(scope, {}).get(key)

    def is_set(self, scope: ScopeKind, key: str) -> bool:
        return key in self.values.get(scope, {})

    def resolve(self, key: str, *, broader_than: ScopeKind | None = None) -> Resolved:
        """``key``'s value following its chain; with ``broader_than``, skip that scope and every
        narrower one (what the key would be if that scope's row were reset)."""
        spec = spec_for(key)
        chain: list[str] = list(spec.chain)
        if key == "policy.ai_review":
            chain.insert(chain.index("repo") + 1, "repo-column")
        if broader_than is not None and broader_than in chain:
            chain = chain[chain.index(broader_than) + 1 :]
        for step in chain:
            if step == "repo-column":
                if self.repo_ai_review_column:
                    return Resolved(key, True, "repo-column", SCOPE_LABELS["repo-column"])
                continue
            scope = cast(ScopeKind, step)
            if scope not in self.ids:
                continue
            layer = self.values.get(scope, {})
            if key in layer:
                return Resolved(key, layer[key], cast(Source, scope), self._scope_detail(scope))
        return fallback(spec, self.settings)

    def _scope_detail(self, scope: ScopeKind) -> str:
        if scope == "org" and self.org_name:
            return f"organization {self.org_name}"
        return SCOPE_LABELS[scope]

    def resolve_all(self) -> dict[str, Any]:
        return {key: self.resolve(key).value for key in CATALOGUE}


def fallback(spec: SettingSpec, settings: Settings) -> Resolved:
    """What a key is when no scope sets it: ``COVERSET_*`` configuration, else the default."""
    if spec.name == "policy.untrusted_pr_policy":
        from coverset.forge.registry import effective_untrusted_pr_policy

        policy = effective_untrusted_pr_policy(settings)
        detail = "COVERSET_UNTRUSTED_PR_POLICY"
        if policy != settings.untrusted_pr_policy:
            detail += " (token-mode default)"
        return Resolved(spec.name, policy, "config", detail)
    if spec.name == "policy.run_drafts":
        return Resolved(spec.name, bool(settings.run_drafts), "config", "COVERSET_RUN_DRAFTS")
    if spec.default is None:
        return Resolved(spec.name, None, "deferred", spec.deferred_to or "[tool.coverset]")
    return Resolved(spec.name, spec.default, "default", SCOPE_LABELS["default"])


def _settings(settings: Settings | None) -> Settings:
    if settings is not None:
        return settings
    from coverset.config import get_settings

    return get_settings()


async def load_layers(
    session: AsyncSession,
    *,
    repo: RepoLike | None = None,
    org: str | None = None,
    user_id: int | str | None = None,
    settings: Settings | None = None,
) -> Layers:
    """Load every scope in play: instance, the repo's org (or ``org``), the repo, the user."""
    ids: dict[ScopeKind, str] = {"instance": ""}
    org_name = org
    if repo is not None:
        ids["repo"] = str(repo.id)
        org_name = org_name or repo.full_name.partition("/")[0]
    if org_name:
        try:
            ids["org"] = normalize_scope_id("org", org_name)
        except SettingError:
            org_name = None
    if user_id is not None:
        ids["user"] = normalize_scope_id("user", user_id)
    created = getattr(repo, "created_at", None) if repo is not None else None
    not_before = {("repo", ids["repo"]): created} if isinstance(created, datetime) else None
    loaded = await load_scopes(session, ids.items(), not_before=not_before)
    return Layers(
        ids=ids,
        values={kind: loaded.get((kind, sid), {}) for kind, sid in ids.items()},
        settings=_settings(settings),
        repo_ai_review_column=bool(repo is not None and repo.ai_review_enabled),
        org_name=org_name,
    )


async def resolve(
    session: AsyncSession,
    key: str,
    *,
    repo: RepoLike | None = None,
    user_id: int | str | None = None,
    settings: Settings | None = None,
) -> Any:
    """The effective value of ``key`` for ``repo`` and ``user_id`` (see the module docstring)."""
    spec_for(key)
    layers = await load_layers(session, repo=repo, user_id=user_id, settings=settings)
    return layers.resolve(key).value


async def resolve_all(
    session: AsyncSession,
    *,
    repo: RepoLike | None = None,
    user_id: int | str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Every key's effective value for ``repo`` and ``user_id``."""
    layers = await load_layers(session, repo=repo, user_id=user_id, settings=settings)
    return layers.resolve_all()


# -- writes ---------------------------------------------------------------------------------------


class AuditActor(Protocol):
    """Who changes a setting: the web layer's ``Principal`` satisfies this; the CLI passes None."""

    @property
    def kind(self) -> str: ...

    @property
    def user_id(self) -> int | None: ...

    @property
    def label(self) -> str: ...


_ACTOR_KINDS: Final[Mapping[str, ActorKind]] = {
    "admin": "admin_token",
    "user": "user",
    "token": "api_token",
    "oidc": "oidc",
}


async def set_value(
    session: AsyncSession,
    key: str,
    raw: object,
    *,
    scope: ScopeKind,
    scope_id: str | int | None = None,
    actor: AuditActor | None = None,
    repo_id: int | None = None,
    commit: bool = True,
) -> Any:
    """Validate and store ``key`` at ``scope``; audit the change; return the stored value.

    Authorization is the caller's job (:attr:`SettingSpec.repo_edit` and the scope rules in the
    module docstring); this only refuses keys the scope does not accept. With ``commit`` (the
    default) the change is committed before the cache is invalidated, so no concurrent reader can
    re-cache the old value between the two. With ``commit=False`` the caller must commit promptly.
    """
    value = spec_for(key).parse(raw)
    await apply_changes(
        session, {key: value}, scope=scope, scope_id=scope_id, actor=actor, repo_id=repo_id, commit=commit
    )
    return value


async def clear_value(
    session: AsyncSession,
    key: str,
    *,
    scope: ScopeKind,
    scope_id: str | int | None = None,
    actor: AuditActor | None = None,
    repo_id: int | None = None,
    commit: bool = True,
) -> bool:
    """Delete ``key``'s row at ``scope`` (back to inheriting); audit it. False if nothing was set."""
    written = await apply_changes(
        session, {key: None}, scope=scope, scope_id=scope_id, actor=actor, repo_id=repo_id, commit=commit
    )
    return key in written


async def apply_changes(
    session: AsyncSession,
    changes: Mapping[str, object],
    *,
    scope: ScopeKind,
    scope_id: str | int | None = None,
    actor: AuditActor | None = None,
    repo_id: int | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Set (raw value) or clear (``None``) several keys at one scope in one transaction.

    Every key and value is validated before anything is written, so a form with one bad field
    changes nothing. Unchanged values are skipped (no row churn, no audit noise). Returns
    ``key -> new value`` for what changed (``None`` for a cleared key).
    """
    sid = normalize_scope_id(scope, scope_id)
    parsed: dict[str, Any] = {}
    for key, raw in changes.items():
        spec = spec_for(key)
        if scope not in spec.scopes:
            raise SettingError(f"{key} cannot be set at {scope} scope")
        parsed[key] = None if raw is None else spec.parse(raw)
    if not parsed:
        return {}
    rows = {
        row.key: row
        for row in await session.scalars(
            select(ScopedSetting).where(
                ScopedSetting.scope_kind == scope,
                ScopedSetting.scope_id == sid,
                ScopedSetting.key.in_(list(parsed)),
            )
        )
    }
    audit_repo = repo_id if repo_id is not None else (int(sid) if scope == "repo" else None)
    # A repo-scope row older than its repository is an orphan of a purged predecessor that reads
    # ignore; it counts as unset here too, so re-entering the same value rewrites (and re-stamps) it.
    born = (
        await session.scalar(select(Repo.created_at).where(Repo.id == int(sid)))
        if scope == "repo" and rows
        else None
    )
    written: dict[str, Any] = {}
    for key, value in parsed.items():
        row = rows.get(key)
        old = _UNSET if row is None else _stored_value(key, row.value, scope)
        stamp = row.updated_at if row is not None else None
        if born is not None and stamp is not None and _aware(stamp) < _aware(born):
            old = _UNSET
        if value is None:
            if row is None:
                continue
            await session.delete(row)
        else:
            if old is not _UNSET and old == value and type(old) is type(value):
                continue
            if row is None:
                row = ScopedSetting(scope_kind=scope, scope_id=sid, key=key)
                session.add(row)
            row.value = {"v": value}
            row.updated_by = actor.user_id if actor is not None else None
            row.updated_at = utc_now()
        written[key] = value
        record(
            session,
            actor,
            "setting_changed",
            target_kind="setting",
            target_id=f"{scope}:{sid}:{key}"[:128],
            repo_id=audit_repo,
            details={
                "op": "clear" if value is None else "set",
                "scope": scope,
                "scope_id": sid,
                "key": key,
                "old": None if old is _UNSET else old,
                "new": value,
            },
        )
    if commit:
        await session.commit()
    else:
        await session.flush()
    _CACHE.invalidate(_engine(session), (scope, sid))
    return written


async def forget_scope(
    session: AsyncSession,
    scope: ScopeKind,
    scope_id: str | int | None,
    *,
    commit: bool = False,
) -> int:
    """Delete every row stored at one scope; return how many went.

    For whoever deletes the thing a scope names: ``coverset repo purge`` (repo scope; the
    repository's API tokens already cascade) and deleting a user (user scope). Neither scope id has
    a foreign key, and SQLite hands a deleted row's id to the next insert, so without this the next
    repository or user would inherit the old one's settings. By default the delete joins the
    caller's transaction (commit it with the parent row's delete). The cache entry is dropped after
    the flush; a reader that re-caches the old rows before the commit lands is harmless, because
    their repository is gone and the next one's ``created_at`` filters them (:func:`load_scopes`).
    Not audited per key: the purge or user deletion is the event worth recording.
    """
    sid = normalize_scope_id(scope, scope_id)
    result = await session.execute(
        delete(ScopedSetting).where(ScopedSetting.scope_kind == scope, ScopedSetting.scope_id == sid)
    )
    if commit:
        await session.commit()
    else:
        await session.flush()
    engine = _engine(session)
    _CACHE.invalidate(engine, (scope, sid))
    if scope == "repo":
        _REPOS.invalidate(engine, int(sid))
    return int(getattr(result, "rowcount", 0) or 0)


def record(
    session: AsyncSession,
    actor: AuditActor | None,
    action: str,
    *,
    target_kind: str | None = None,
    target_id: str | None = None,
    repo_id: int | None = None,
    details: Mapping[str, Any] | None = None,
) -> None:
    """Add one ``audit_log`` row to ``session`` (committed with the change it describes).

    Uses the shared :func:`coverset.web.audit.record_audit` (which redacts secrets in ``details``
    and records the client address) when it is importable; otherwise writes the row directly, so
    the CLI and an install without the web layer still leave a trail. ``actor`` None is the CLI.
    """
    try:
        from coverset.web.audit import record_audit
        from coverset.web.principal import Principal
    except ImportError:  # pragma: no cover - the web layer ships with coverset today
        record_audit = None
        Principal = None  # type: ignore[assignment,misc]  # noqa: N806
    if record_audit is not None and Principal is not None and (actor is None or isinstance(actor, Principal)):
        record_audit(
            session,
            actor,
            action,
            target_kind=target_kind,
            target_id=target_id,
            repo_id=repo_id,
            details=dict(details or {}),
        )
        return
    kind: ActorKind = _ACTOR_KINDS.get(actor.kind, "system") if actor is not None else "system"
    session.add(
        AuditEvent(
            actor_kind=kind,
            actor_id=f"user:{actor.user_id}" if actor is not None and actor.user_id is not None else None,
            actor_label=((actor.label or actor.kind) if actor is not None else "cli")[:255],
            action=action[:64],
            target_kind=target_kind,
            target_id=target_id,
            repo_id=repo_id,
            details=dict(details or {}),
        )
    )


# -- the orchestrator's view ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Policy:
    """The effective ``policy.*`` values for one repository (what intake, planner and verdict read).

    ``fail_fast``, ``max_shards`` and ``target_shard_seconds`` are None when no scope sets them: the
    repository's ``[tool.coverset]`` decides (see :meth:`apply`).
    """

    untrusted_pr_policy: UntrustedPrPolicy
    run_drafts: bool
    ai_review: bool
    fail_fast: FailFast | None
    max_shards: int | None
    target_shard_seconds: int | None
    test_tax_threshold: float
    public_report: bool
    sources: Mapping[str, str] = field(default_factory=dict)
    """``key -> source`` (``repo``, ``org``, ``instance``, ``config``, ``default``...) for logs."""

    def apply[C](self, config: C, *, instance_max_shards: int) -> C:
        """``config`` (a ``CoversetConfig``) with the scoped policy applied.

        ``fail_fast`` and ``target_shard_seconds`` replace the file's values when a scope sets them:
        ``[tool.coverset]`` comes from the pull request's head, which its author controls, while
        scoped policy is set by the repository's writers and admins. ``max_shards`` is an upper
        bound, not a replacement: the result is the smallest of the scoped value, the file's value
        and ``instance_max_shards`` (``COVERSET_MAX_SHARDS``). Only *raising* the shard count costs
        the instance anything, so a lower value from the PR head is harmless and is kept (an
        instance-wide cap of 8 must not push a repository that asks for 2 up to 8).
        """
        if not dataclasses.is_dataclass(config) or isinstance(config, type):
            return config
        names = {f.name for f in dataclasses.fields(config)}
        changes: dict[str, Any] = {}
        if self.fail_fast is not None and "fail_fast" in names:
            changes["fail_fast"] = self.fail_fast
        if self.target_shard_seconds is not None and "target_shard_seconds" in names:
            changes["target_shard_seconds"] = float(self.target_shard_seconds)
        if "max_shards" in names:
            own = int(config.max_shards)  # type: ignore[attr-defined]
            capped = own if self.max_shards is None else min(own, self.max_shards)
            changes["max_shards"] = max(1, min(capped, instance_max_shards))
        return cast(C, dataclasses.replace(config, **changes)) if changes else config


def policy_from_layers(layers: Layers) -> Policy:
    """Build a :class:`Policy` from already-loaded layers."""
    resolved = {key: layers.resolve(key) for key in POLICY_KEYS}

    def value(key: str) -> Any:
        return resolved[f"policy.{key}"].value

    return Policy(
        untrusted_pr_policy=value("untrusted_pr_policy"),
        run_drafts=bool(value("run_drafts")),
        ai_review=bool(value("ai_review")),
        fail_fast=value("fail_fast"),
        max_shards=value("max_shards"),
        target_shard_seconds=value("target_shard_seconds"),
        test_tax_threshold=float(value("test_tax_threshold")),
        public_report=bool(value("public_report")),
        sources={key: r.source for key, r in resolved.items()},
    )


async def effective_policy(session: AsyncSession, repo: RepoLike, settings: Settings | None = None) -> Policy:
    """The repository's policy: ``repo > org > instance > COVERSET_* > default`` (cached reads).

    ``settings`` supplies the ``COVERSET_*`` fallbacks (the process-wide settings when omitted).
    """
    layers = await load_layers(session, repo=repo, settings=settings)
    return policy_from_layers(layers)


# -- the UI's view --------------------------------------------------------------------------------


async def repo_stub(session: AsyncSession, repo_id: int) -> RepoStub | None:
    """A cached :class:`RepoStub` for ``repo_id`` (the UI resolves view preferences on every page,
    and only needs the repository's id and owner, which change rarely), or None if it is unknown."""
    engine = _engine(session)
    cached = _REPOS.get(engine, repo_id)
    if cached is not None:
        return cached
    row = (
        await session.execute(
            select(Repo.full_name, Repo.ai_review_enabled, Repo.created_at).where(Repo.id == repo_id)
        )
    ).first()
    if row is None:
        return None
    stub = RepoStub(id=repo_id, full_name=row[0], ai_review_enabled=bool(row[1]), created_at=row[2])
    _REPOS.put(engine, repo_id, stub)
    return stub


class ViewPrefs(dict[str, Any]):
    """The four ``view.*`` values; also readable by short name (``prefs.highlight``) in templates."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[f"view.{name}"]
        except KeyError:
            raise AttributeError(name) from None


def default_view_prefs() -> ViewPrefs:
    """Built-in defaults of every view key (what a page shows when the database is unavailable)."""
    return ViewPrefs({key: CATALOGUE[key].default for key in VIEW_KEYS})


async def view_prefs(
    session: AsyncSession,
    *,
    repo: RepoLike | None,
    user_id: int | str | None,
    settings: Settings | None = None,
) -> ViewPrefs:
    """The reader's view preferences: ``user > repo > org > instance > default``."""
    layers = await load_layers(session, repo=repo, user_id=user_id, settings=settings)
    return ViewPrefs({key: layers.resolve(key).value for key in VIEW_KEYS})


# -- documentation --------------------------------------------------------------------------------


def _type_text(spec: SettingSpec) -> str:
    if spec.kind == "enum":
        return " \\| ".join(f"`{c}`" for c in spec.choices)
    if spec.kind == "bool":
        return "bool"
    bounds = f" {_num(spec.minimum)}..{_num(spec.maximum)}" if spec.minimum is not None else ""
    return f"{spec.kind}{bounds}"


def _value_text(spec: SettingSpec, value: Any) -> str:
    if spec.kind == "bool":
        return "true" if value else "false"
    return str(value) if spec.kind == "enum" else _num(value)


def _default_text(spec: SettingSpec) -> str:
    if spec.config_env:
        return f"`{spec.config_env}` (default `{_value_text(spec, spec.default)}`)"
    if spec.default is None:
        return spec.deferred_to or "[tool.coverset]"
    return f"`{_value_text(spec, spec.default)}`"


def _editors_text(spec: SettingSpec) -> str:
    parts = []
    if "user" in spec.scopes:
        parts.append("user: the user")
    if "repo" in spec.scopes:
        parts.append(f"repo: repository {spec.repo_edit}")
    if "org" in spec.scopes or "instance" in spec.scopes:
        parts.append("org, instance: instance admin")
    return "; ".join(parts)


def catalogue_markdown() -> str:
    """The keys table of ``docs/settings.md`` (a test keeps the two identical)."""
    lines = [
        "| Key | Values | Default | Scopes | Who may edit | Meaning |",
        "|---|---|---|---|---|---|",
    ]
    for spec in CATALOGUE.values():
        scopes = ", ".join(s for s in SCOPES if s in spec.scopes)
        lines.append(
            f"| `{spec.name}` | {_type_text(spec)} | {_default_text(spec)} | {scopes} | "
            f"{_editors_text(spec)} | {spec.description} |"
        )
    return "\n".join(lines)


def describe_sources(resolved: Sequence[Resolved]) -> list[str]:
    """``key = value  (source)`` lines for the CLI's ``settings list``."""
    width = max((len(r.key) for r in resolved), default=0)
    lines = []
    for r in resolved:
        shown = "unset" if r.source == "deferred" else r.display
        lines.append(f"{r.key:<{width}}  {shown:<28}  ({r.detail})")
    return lines
