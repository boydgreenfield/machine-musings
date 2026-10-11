"""Secret redaction for logs, stored run events and captured worker output.

:func:`redact` masks well-known credential shapes (GitHub tokens, Anthropic keys, Modal tokens,
credentials in any URL, PEM private keys, bearer headers, ``NAME=value`` pairs whose name says
secret, token, password or API key) plus every live secret registered with :func:`register_secret`. :class:`RedactingFilter` applies it to log records, and
:func:`configure_logging` installs that filter wherever records are emitted.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from collections.abc import Iterable
from typing import TYPE_CHECKING

from pydantic import SecretStr

if TYPE_CHECKING:
    from coverset.config import Settings

MIN_SECRET_CHARS = 8
"""Floor for :func:`register_secret` and ``extra_secrets``: shorter values would shred text.

:func:`configure_logging` applies it to the settings' own secrets too; ``COVERSET_GITHUB_TOKEN``,
the webhook secret and the admin token have longer minimums of their own (``config.py``).
"""

ENV_SECRETS = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")
"""Process environment variables whose live values :func:`configure_logging` registers."""

_SECRET_NAME = (
    r"[A-Za-z0-9_]*?(?:secret[_-]?access[_-]?key|modal[_-]token[_-](?:id|secret)|_secret|_token"
    r"|_api[_-]?key|_password)"
)
"""Names whose value is a secret: ``AWS_SECRET_ACCESS_KEY``, ``MODAL_TOKEN_ID``, ``*_SECRET``, ..."""

_TOKEN_START = r"(?<![A-Za-z0-9])"
"""Start of a token even when glued to a preceding ``_`` (``GH_TOKEN_ghs_...``)."""

PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
        ),
        "[REDACTED PRIVATE KEY]",
    ),
    (re.compile(r"(?i)(x-access-token:)[^@\s]+@"), r"\1[REDACTED]@"),
    (re.compile(_TOKEN_START + r"(gh[opsur]_)[A-Za-z0-9._-]{20,}"), r"\1[REDACTED]"),
    (re.compile(_TOKEN_START + r"(github_pat_)[A-Za-z0-9_]{20,}"), r"\1[REDACTED]"),
    (re.compile(_TOKEN_START + r"(sk-ant-)[A-Za-z0-9_-]{8,}"), r"\1[REDACTED]"),
    # Modal token ids and secrets.
    (re.compile(_TOKEN_START + r"(a[ks]-)[A-Za-z0-9]{20,}"), r"\1[REDACTED]"),
    # user:password@ in any URL (x-access-token URLs were handled above).
    (re.compile(r"(://)[^/\s:@]+:(?!\[REDACTED\]@)[^/\s@]+@"), r"\1[REDACTED]@"),
    # NAME=value and "NAME": "value" where the name says it holds a secret.
    (re.compile(r"(?i)\b(" + _SECRET_NAME + r"[ \t]*=[ \t]*)(?!\[REDACTED\])[^\s'\"&;,]+"), r"\1[REDACTED]"),
    (
        re.compile(r"""(?i)(['"]""" + _SECRET_NAME + r"""['"]\s*:\s*['"])(?!\[REDACTED\])[^'"]+"""),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"""(?i)\b(authorization['"]?\s*[:=]\s*['"]?(?:bearer|token|basic)\s+)[^\s"',;}]+"""),
        r"\1[REDACTED]",
    ),
    # GitHub App manifest codes in /setup/github/callback request lines (hypercorn's access log).
    (re.compile(r"(/setup/github/callback\?(?:[^\s\"'#]*?[&;])?code=)[^\s\"'&;#]+"), r"\1[REDACTED]"),
)
"""Credential shapes masked in all text, applied in order (PEM blocks first)."""

REDACTED = "[REDACTED]"
_HANDLER_MARK = "_coverset_redacting_handler"
_NAMED_LOGGERS = ("hypercorn.error", "hypercorn.access", "quart.app", "quart.serving")

_lock = threading.Lock()
_secrets: set[str] = set()
_secret_re: re.Pattern[str] | None = None


def register_secret(value: str | SecretStr | None, *, min_chars: int = MIN_SECRET_CHARS) -> None:
    """Mask ``value`` wherever it appears in redacted text from now on.

    Empty values and values shorter than ``min_chars`` are ignored.
    """
    global _secret_re
    raw = value.get_secret_value() if isinstance(value, SecretStr) else value
    if not raw or len(raw) < min_chars:
        return
    with _lock:
        if raw in _secrets:
            return
        _secrets.add(raw)
        _secret_re = _alternation(_secrets)


def clear_registered_secrets() -> None:
    """Forget every registered secret (tests and settings reloads)."""
    global _secret_re
    with _lock:
        _secrets.clear()
        _secret_re = None


def _alternation(values: Iterable[str]) -> re.Pattern[str] | None:
    ordered = sorted({v for v in values if v}, key=len, reverse=True)
    if not ordered:
        return None
    return re.compile("|".join(re.escape(v) for v in ordered))


def redact(text: str, extra_secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with credential shapes, registered secrets and ``extra_secrets`` masked."""
    if not text:
        return text
    extra = _alternation(v for v in extra_secrets if len(v) >= MIN_SECRET_CHARS)
    if extra is not None:
        text = extra.sub(REDACTED, text)
    registered = _secret_re
    if registered is not None:
        text = registered.sub(REDACTED, text)
    for pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Rewrite each record's message, exception text and stack info through :func:`redact`.

    The record is never dropped; arguments are folded into the message so a secret passed as a
    ``%s`` argument is masked too.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except (TypeError, ValueError):
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return True


def _ensure_filter(target: logging.Logger | logging.Handler) -> None:
    if not any(isinstance(f, RedactingFilter) for f in target.filters):
        target.addFilter(RedactingFilter())


def configure_logging(settings: Settings) -> None:
    """Register the settings' live secrets and the Modal tokens in the process environment (each at
    least :data:`MIN_SECRET_CHARS` long) and install :class:`RedactingFilter`.

    Logger-level filters only see records created on that logger, so the filter goes on the root
    logger, on the hypercorn/quart loggers, and on every handler attached to any of them. A single
    stderr handler is added to the root logger once (idempotent across ``create_app`` calls).
    """
    for value in settings.secret_values():
        register_secret(value)
    for name in ENV_SECRETS:
        register_secret(os.environ.get(name))
    root = logging.getLogger()
    if not any(getattr(h, _HANDLER_MARK, False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        setattr(handler, _HANDLER_MARK, True)
        root.addHandler(handler)
    if settings.env != "test" and root.level > logging.INFO:
        root.setLevel(logging.INFO)
    for logger in (root, *(logging.getLogger(name) for name in _NAMED_LOGGERS)):
        _ensure_filter(logger)
        for handler in logger.handlers:
            _ensure_filter(handler)
