"""Webhook HMAC signatures (``X-Hub-Signature-256``) and the fail-closed secret policy."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from typing import Literal

from githubkit import webhooks

from coverset.config import Settings
from coverset.forge.base import header

TEST_WEBHOOK_SECRET = "coverset-test-secret"
"""The secret the recorded fixtures are signed with; accepted only when ``COVERSET_ENV=test``."""

SIGNATURE_HEADER = "X-Hub-Signature-256"
_SIGNATURE_PREFIX = "sha256="
_SIGNATURE_LEN = len(_SIGNATURE_PREFIX) + 64

SecretProblem = Literal["unset", "test_secret_outside_test"]


def sign_body(secret: str, raw_body: bytes) -> str:
    """``sha256=<hex>`` HMAC of ``raw_body`` (exactly what GitHub sends)."""
    return _SIGNATURE_PREFIX + hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify_signature(secret: str | None, headers: Mapping[str, str], raw_body: bytes) -> bool:
    """Constant-time check of ``X-Hub-Signature-256`` over the raw bytes; False without a secret.

    Only the SHA-256 header is honoured (never the legacy SHA-1 ``X-Hub-Signature``).
    """
    if not secret:
        return False
    signature = (header(headers, SIGNATURE_HEADER) or "").strip()
    if len(signature) != _SIGNATURE_LEN or not signature.startswith(_SIGNATURE_PREFIX):
        return False
    return webhooks.verify(secret, raw_body, signature)


def webhook_secret(settings: Settings) -> tuple[str | None, SecretProblem | None]:
    """The usable webhook secret, or None and why not (webhooks then fail closed with 503)."""
    secret = settings.github_webhook_secret.get_secret_value() if settings.github_webhook_secret else ""
    if not secret:
        return None, "unset"
    if settings.env != "test" and hmac.compare_digest(secret, TEST_WEBHOOK_SECRET):
        return None, "test_secret_outside_test"
    return secret, None
