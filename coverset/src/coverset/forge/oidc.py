"""Verify GitHub Actions OIDC tokens presented by ``coverset-ci wait``.

RS256 only, issuer ``https://token.actions.githubusercontent.com``, audience = coverset's
``COVERSET_OIDC_AUDIENCE``, 60 s leeway, and the identity claims the API binds
(``repository_id``, ``repository_owner_id``) are required. Signing keys come from GitHub's JWKS
through a cached :class:`jwt.PyJWKClient` wrapped in :class:`GuardedJwkClient` (tests pass their
own ``jwk_client``).

``PyJWKClient`` refetches the JWKS for every unknown ``kid`` under one client-wide lock, and its
refresh cooldown starts only after a *successful* fetch. While GitHub's JWKS is slow or down, every
token with a random ``kid`` would hold that lock for the full fetch timeout and stall every CI
job's verification. The guard answers known kids from its own cache, and refuses kids that just
failed and all unknown kids during a fetch outage, for :data:`NEGATIVE_CACHE_S`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import jwt
from jwt import PyJWK, PyJWKClient

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = f"{ISSUER}/.well-known/jwks"
ALGORITHMS = ["RS256"]
LEEWAY_S = 60
REQUIRED_CLAIMS = ["exp", "iat", "nbf", "iss", "aud", "sub", "repository_id", "repository_owner_id"]
MAX_TOKEN_CHARS = 16_384
NEGATIVE_CACHE_S = 30.0
"""How long an unknown ``kid`` or a failed JWKS fetch is answered from the negative cache."""
KNOWN_KEY_TTL_S = 3600.0
MAX_CACHED_KIDS = 256
MAX_KID_CHARS = 256


class OidcError(Exception):
    """The token is malformed, unsigned, mis-signed, expired or meant for someone else."""


class JwkClient(Protocol):
    """The part of :class:`jwt.PyJWKClient` the verifier uses."""

    def get_signing_key_from_jwt(self, token: str) -> PyJWK: ...


@dataclass(frozen=True, slots=True)
class Claims:
    """The verified identity of a GitHub Actions job."""

    sub: str
    repository_id: int
    repository_owner_id: int
    repository: str | None = None
    ref: str | None = None
    sha: str | None = None
    event_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)


class GuardedJwkClient:
    """Positive and negative caching in front of a :class:`jwt.PyJWKClient` (see the module doc)."""

    def __init__(
        self,
        inner: JwkClient,
        *,
        negative_ttl_s: float = NEGATIVE_CACHE_S,
        known_ttl_s: float = KNOWN_KEY_TTL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.inner = inner
        self.negative_ttl_s = negative_ttl_s
        self.known_ttl_s = known_ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._known: dict[str, tuple[PyJWK, float]] = {}
        self._unknown: dict[str, float] = {}
        self._outage_until = 0.0

    def get_signing_key_from_jwt(self, token: str) -> PyJWK:
        """The key for ``token``'s ``kid``; raises :class:`jwt.PyJWKClientError` fast for kids that
        just failed, and for any unknown kid while the JWKS endpoint is failing."""
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except jwt.InvalidTokenError as exc:
            raise jwt.PyJWKClientError(f"malformed token header: {exc}") from None
        if not isinstance(kid, str) or not kid or len(kid) > MAX_KID_CHARS:
            raise jwt.PyJWKClientError("token header has no usable kid")
        now = self._clock()
        with self._lock:
            known = self._known.get(kid)
            if known is not None and known[1] > now:
                return known[0]
            if self._outage_until > now:
                raise jwt.PyJWKClientConnectionError("JWKS endpoint failing; retry shortly")
            if self._unknown.get(kid, 0.0) > now:
                raise jwt.PyJWKClientError(f"no signing key for kid {kid!r} (cached)")
        try:
            key = self.inner.get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError:
            with self._lock:
                self._outage_until = self._clock() + self.negative_ttl_s
            raise
        except jwt.PyJWKClientError:
            with self._lock:
                self._remember(self._unknown, kid, self._clock() + self.negative_ttl_s)
            raise
        with self._lock:
            self._remember(self._known, kid, (key, self._clock() + self.known_ttl_s))
        return key

    def _remember[V](self, cache: dict[str, V], kid: str, value: V) -> None:
        cache.pop(kid, None)
        cache[kid] = value
        while len(cache) > MAX_CACHED_KIDS:
            cache.pop(next(iter(cache)))


_clients: dict[str, GuardedJwkClient] = {}
_clients_lock = threading.Lock()


def _jwk_client(jwks_url: str) -> GuardedJwkClient:
    with _clients_lock:
        client = _clients.get(jwks_url)
        if client is None:
            inner = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600, timeout=5)
            client = _clients[jwks_url] = GuardedJwkClient(inner)
        return client


def _int_claim(claims: dict[str, Any], name: str) -> int:
    value = claims.get(name)
    if isinstance(value, bool):
        raise OidcError(f"claim {name} is not numeric")
    try:
        return int(str(value))
    except ValueError:
        raise OidcError(f"claim {name} is not numeric") from None


def verify_actions_oidc(
    token: str,
    *,
    audience: str,
    jwks_url: str = JWKS_URL,
    jwk_client: JwkClient | None = None,
) -> Claims:
    """Verify ``token`` and return its claims; raise :class:`OidcError` on any problem.

    The header's ``alg`` is checked before any key lookup, so ``none`` and HMAC tokens are refused
    without touching the network. Blocking (key fetches use urllib): call it via ``asyncio.to_thread``.
    """
    if not token or len(token) > MAX_TOKEN_CHARS:
        raise OidcError("missing or oversized token")
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        raise OidcError("malformed token") from None
    if header.get("alg") not in ALGORITHMS:
        raise OidcError(f"algorithm {header.get('alg')!r} is not accepted")
    client = jwk_client if jwk_client is not None else _jwk_client(jwks_url)
    try:
        signing_key = client.get_signing_key_from_jwt(token)
    except jwt.PyJWKClientError as exc:
        raise OidcError(f"no signing key: {exc}") from None
    except jwt.InvalidTokenError:
        raise OidcError("malformed token") from None
    try:
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=ALGORITHMS,
            audience=audience,
            issuer=ISSUER,
            leeway=LEEWAY_S,
            options={"require": REQUIRED_CLAIMS},
        )
    except jwt.InvalidTokenError as exc:
        raise OidcError(f"invalid token: {type(exc).__name__}") from None

    def text(name: str) -> str | None:
        value = claims.get(name)
        return value if isinstance(value, str) else None

    return Claims(
        sub=str(claims["sub"]),
        repository_id=_int_claim(claims, "repository_id"),
        repository_owner_id=_int_claim(claims, "repository_owner_id"),
        repository=text("repository"),
        ref=text("ref"),
        sha=text("sha"),
        event_name=text("event_name"),
        raw=claims,
    )
