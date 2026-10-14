"""Who is making this request, and what may they do.

Every view that touches a repository goes through :func:`require_repo` (or :func:`visible_repo_ids`
for listings). The :class:`Principal` is resolved once per request by the access guard in
:mod:`coverset.web.auth` and stored on ``g``; views never look at credentials themselves.

Kinds:

* ``admin``: the ``COVERSET_ADMIN_TOKEN`` bearer or its login cookie, or open mode on a loopback
  bind. Sees and changes everything.
* ``user``: a person signed in through GitHub (``users`` row). Sees the repositories GitHub says
  they may read (``repo_access``), changes the ones they may push to; instance admins
  (``users.role = admin``) see everything.
* ``token``: an ``api_tokens`` bearer. A personal token acts as its user within its scopes; a
  repository token acts on its one repository within its scopes.
* ``oidc``: a GitHub Actions token verified by :mod:`coverset.forge.oidc`, bound to one repository.
* ``public``: no credentials. May reach only routes that authorize ``public`` callers themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from quart import g
from werkzeug.exceptions import Forbidden, NotFound

PrincipalKind = Literal["admin", "user", "token", "oidc", "public"]
Permission = Literal["read", "write", "admin"]

_ORDER: dict[str, int] = {"read": 0, "write": 1, "admin": 2}


def permission_at_least(have: str | None, want: Permission) -> bool:
    """True when ``have`` (a :data:`Permission` or None) grants ``want``."""
    return have is not None and _ORDER.get(have, -1) >= _ORDER[want]


@dataclass(frozen=True, slots=True)
class Principal:
    """The resolved identity of the current request."""

    kind: PrincipalKind
    user_id: int | None = None
    login: str | None = None
    label: str = ""
    """Short human-readable description for audit rows (``admin token``, ``@login``, ``token abc…``)."""
    instance_admin: bool = False
    """True for the admin token, open mode and users with ``role = admin``."""
    scopes: frozenset[str] = field(default_factory=frozenset)
    """For ``token`` principals: ``read``, ``write``, ``upload``, ``admin``."""
    repo_permissions: dict[int, str] = field(default_factory=dict)
    """``repo_id -> permission`` for ``user`` and personal-token principals (from ``repo_access``)."""
    bound_repo_id: int | None = None
    """For repository tokens and OIDC callers: the one repository they may act on."""
    token_id: int | None = None
    session_id: str | None = None

    @property
    def is_public(self) -> bool:
        return self.kind == "public"

    def repo_permission(self, repo_id: int) -> str | None:
        """The permission this principal holds on ``repo_id`` (None = cannot see it)."""
        if self.instance_admin:
            return "admin"
        if self.bound_repo_id is not None:
            if repo_id != self.bound_repo_id:
                return None
            if self.kind == "oidc":
                return "read"
            return "write" if "write" in self.scopes or "upload" in self.scopes else "read"
        permission = self.repo_permissions.get(repo_id)
        if permission is None or self.kind != "token":
            return permission
        # A personal token never exceeds its scopes.
        if "admin" in self.scopes:
            return permission
        if "write" in self.scopes:
            return "write" if permission_at_least(permission, "write") else permission
        return "read"

    def can(self, repo_id: int, want: Permission) -> bool:
        return permission_at_least(self.repo_permission(repo_id), want)


PUBLIC = Principal(kind="public", label="public")
ADMIN = Principal(kind="admin", label="admin token", instance_admin=True)


def current_principal() -> Principal:
    """The principal the access guard resolved for this request (``PUBLIC`` before it ran)."""
    principal = g.get("coverset_principal")
    return principal if principal is not None else PUBLIC


def set_principal(principal: Principal) -> None:
    g.coverset_principal = principal


def require_repo(repo_id: int, want: Permission = "read") -> Principal:
    """Return the principal if it may ``want`` on ``repo_id``.

    Raises 404 (never 403) when the principal cannot see the repository at all, so a repository's
    existence is not revealed, and 403 when it can see it but lacks ``want``.
    """
    principal = current_principal()
    permission = principal.repo_permission(repo_id)
    if permission is None:
        raise NotFound()
    if not permission_at_least(permission, want):
        raise Forbidden()
    return principal


def require_instance_admin() -> Principal:
    """Return the principal if it is an instance admin, else 403 (404 for public callers)."""
    principal = current_principal()
    if principal.is_public:
        raise NotFound()
    if not principal.instance_admin:
        raise Forbidden()
    return principal


def visible_repo_ids(all_repo_ids: list[int]) -> list[int]:
    """Filter a repository id list to the ones the current principal may read (admins: all)."""
    principal = current_principal()
    if principal.instance_admin:
        return list(all_repo_ids)
    return [rid for rid in all_repo_ids if principal.can(rid, "read")]
