"""Secret masking in free text and log records."""

from __future__ import annotations

import logging
import secrets
import string
import sys

import pytest

from coverset.config import Settings
from coverset.logging import (
    REDACTED,
    RedactingFilter,
    clear_registered_secrets,
    configure_logging,
    redact,
    register_secret,
)

pytestmark = pytest.mark.security

ALNUM = string.ascii_letters + string.digits


def random_token(prefix: str, length: int) -> str:
    return prefix + "".join(secrets.choice(ALNUM) for _ in range(length - len(prefix)))


def test_long_installation_token_masked() -> None:
    token = random_token("ghs_", 520)
    text = f"cloning with token {token} done"
    out = redact(text)
    assert token not in out
    assert token[4:40] not in out
    assert out == "cloning with token ghs_[REDACTED] done"


@pytest.mark.parametrize(
    "secret",
    [
        random_token("ghp_", 40),
        random_token("github_pat_", 93),
        "sk-ant-api03-" + random_token("", 90),
    ],
)
def test_known_token_shapes_masked(secret: str) -> None:
    out = redact(f"key={secret};")
    assert secret not in out
    assert REDACTED in out
    assert out.startswith("key=") and out.endswith(";")


def test_credentials_in_git_url_masked() -> None:
    url = "https://x-access-token:v1.0123456789abcdef@github.com/acme/app.git"
    out = redact(f"git fetch {url}")
    assert "v1.0123456789abcdef" not in out
    assert out == "git fetch https://x-access-token:[REDACTED]@github.com/acme/app.git"


def test_pem_block_masked_and_surroundings_kept() -> None:
    body = "\n".join(random_token("", 64) for _ in range(5))
    pem = f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----"
    out = redact(f"before\n{pem}\nafter")
    assert body.splitlines()[0] not in out
    assert out == "before\n[REDACTED PRIVATE KEY]\nafter"


def test_truncated_pem_block_masked_to_end() -> None:
    out = redact("key: -----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC")
    assert "MIIEvQ" not in out


JWT = "eyJhbGciOiJSUzI1NiJ9.eyJyZXBvc2l0b3J5X2lkIjoiNDIifQ.c2lnbmF0dXJl"


@pytest.mark.parametrize(
    "text",
    [
        f"Authorization: Bearer {JWT}",
        f"authorization:bearer {JWT}",
        str({"Authorization": f"Bearer {JWT}", "Accept": "application/json"}),
        f'{{"authorization": "Bearer {JWT}"}}',
        f"Authorization=Token {JWT}",
        f"headers={{'authorization': 'Basic {JWT}'}}",
    ],
)
def test_authorization_header_masked(text: str) -> None:
    out = redact(text)
    assert JWT not in out
    assert REDACTED in out


def test_dict_header_masking_keeps_structure() -> None:
    out = redact(str({"Authorization": f"Bearer {JWT}", "Accept": "application/json"}))
    assert out == "{'Authorization': 'Bearer [REDACTED]', 'Accept': 'application/json'}"


@pytest.mark.parametrize("prefix", ["GH_TOKEN_", "token_", "x_"])
@pytest.mark.parametrize("shape", ["ghs_", "github_pat_", "sk-ant-"])
def test_token_glued_to_identifier_masked(prefix: str, shape: str) -> None:
    secret = random_token(shape, 60)
    out = redact(f"{prefix}{secret}")
    assert secret not in out
    assert out == f"{prefix}{shape}{REDACTED}"


def test_registered_and_extra_secrets_masked() -> None:
    dynamic = "dyn-" + random_token("", 24)
    register_secret(dynamic)
    assert redact(f"value {dynamic}!") == f"value {REDACTED}!"
    one_off = "one-off-secret-value"
    assert redact(f"{one_off} and {dynamic}", extra_secrets=[one_off]) == f"{REDACTED} and {REDACTED}"
    clear_registered_secrets()
    assert redact(dynamic) == dynamic


def test_short_registered_values_ignored() -> None:
    register_secret("abc")
    register_secret("")
    register_secret(None)
    assert redact("abc abc") == "abc abc"
    assert redact("abc", extra_secrets=["abc"]) == "abc"


def test_short_registered_value_masked_when_floor_lowered() -> None:
    register_secret("shhh12", min_chars=1)
    assert redact("secret=shhh12") == f"secret={REDACTED}"


def test_configure_logging_skips_settings_secrets_too_short_to_redact() -> None:
    # A 1-7 character value would shred every log line it happens to occur in.
    configure_logging(Settings.for_tests(github_app_client_secret="shhh12"))
    assert redact("the word shhh12 stays") == "the word shhh12 stays"
    configure_logging(Settings.for_tests(github_app_client_secret="client-secret-0451"))
    assert redact("x client-secret-0451 y") == f"x {REDACTED} y"


@pytest.mark.parametrize(
    ("text", "secret", "expected"),
    [
        (
            "MODAL_TOKEN_SECRET=as-abcdefghijklmnopqrstuvwxyz",
            "abcdefghijklmnopqrstuvwxyz",
            f"MODAL_TOKEN_SECRET={REDACTED}",
        ),
        (
            "token ak-ABCDEFGHIJKLMNOPQRSTUV0123 used",
            "ABCDEFGHIJKLMNOPQRSTUV0123",
            f"token ak-{REDACTED} used",
        ),
        ("https://user:pass@github.com/x.git", "user:pass", f"https://{REDACTED}@github.com/x.git"),
        (
            "fetch http://bob:s3cr3t-pw@10.0.0.1:8080/r",
            "s3cr3t-pw",
            f"fetch http://{REDACTED}@10.0.0.1:8080/r",
        ),
        (
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "wJalrXUtnFEMI",
            f"AWS_SECRET_ACCESS_KEY={REDACTED}",
        ),
        ("aws_secret_access_key = abcdef", "abcdef", f"aws_secret_access_key = {REDACTED}"),
        (
            "COVERSET_GITHUB_WEBHOOK_SECRET=hunter2hunter2 x",
            "hunter2",
            f"COVERSET_GITHUB_WEBHOOK_SECRET={REDACTED} x",
        ),
        ("GET /x?access_token=abc123&y=1", "abc123", f"GET /x?access_token={REDACTED}&y=1"),
        ('{"MODAL_TOKEN_ID": "ak-shortish"}', "shortish", f'{{"MODAL_TOKEN_ID": "{REDACTED}"}}'),
        ("ANTHROPIC_API_KEY=sk-ant-admin01-xxxxxxxxxxxx", "admin01", f"ANTHROPIC_API_KEY={REDACTED}"),
    ],
)
def test_modal_tokens_url_credentials_and_secret_named_values_masked(
    text: str, secret: str | None, expected: str
) -> None:
    out = redact(text)
    assert out == expected
    if secret is not None:
        assert secret not in out


def test_configure_logging_registers_modal_tokens_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODAL_TOKEN_ID", "ak-" + "i" * 22)
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "unusual-modal-secret-shape")
    configure_logging(Settings.for_tests())
    assert (
        redact("id ak-" + "i" * 22 + " secret unusual-modal-secret-shape")
        == f"id {REDACTED} secret {REDACTED}"
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain log line: 212 tests passed in 1.8 s",
        "ghost_town and gh_ and sk-ant and github_pat are words",
        "x-access-token is a username, not a secret",
        "as-is and ak-47 and as-built are words",
        "see https://github.com/acme/app and http://[::1]:8000/runs",
        "invalid_token: bad signature",
        "def parse_duration(text: str) -> int: ...",
    ],
)
def test_ordinary_text_unchanged(text: str) -> None:
    assert redact(text) == text


def _record(msg: str, *args: object, exc_info: bool = False) -> logging.LogRecord:
    return logging.LogRecord(
        "coverset.test", logging.ERROR, __file__, 1, msg, args, sys.exc_info() if exc_info else None
    )


def test_filter_masks_message_arguments_and_tracebacks() -> None:
    token = random_token("ghs_", 120)
    record = _record("cloning with %s", token)
    assert RedactingFilter().filter(record) is True
    assert token not in record.getMessage()
    try:
        raise RuntimeError(f"auth failed for {token}")
    except RuntimeError:
        failing = _record("boom", exc_info=True)
    RedactingFilter().filter(failing)
    assert failing.exc_text is not None
    assert token not in failing.exc_text
    assert "RuntimeError" in failing.exc_text


def test_configure_logging_registers_settings_secrets_and_installs_filters() -> None:
    admin = "admin-" + random_token("", 40)
    configure_logging(Settings.for_tests(admin_token=admin))
    assert redact(f"token={admin}") == f"token={REDACTED}"
    for name in ("", "hypercorn.error", "hypercorn.access", "quart.app"):
        logger = logging.getLogger(name)
        assert any(isinstance(f, RedactingFilter) for f in logger.filters)
    configure_logging(Settings.for_tests())
    root_filters = [f for f in logging.getLogger().filters if isinstance(f, RedactingFilter)]
    assert len(root_filters) == 1


def test_records_from_named_loggers_reach_handlers_redacted(caplog: pytest.LogCaptureFixture) -> None:
    configure_logging(Settings.for_tests())
    token = random_token("ghp_", 40)
    with caplog.at_level(logging.INFO, logger="hypercorn.error"):
        logging.getLogger("hypercorn.error").info("token %s", token)
    assert caplog.records
    assert token not in caplog.text
