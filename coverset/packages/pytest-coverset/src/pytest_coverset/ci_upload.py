"""``coverset-ci upload``: send the bundle ``pytest --coverset`` wrote to a coverset control plane.

Upload mode is the shortest way onto coverset: the tests keep running in the repository's own CI
(GitHub Actions or anything else), and this command ships what the plugin left in ``.coverset/``
to ``POST /v1/bundles``. The server ingests it, computes patch coverage, input-class gaps and
invariants, and posts the sticky comment and check run. Nothing here needs more than the standard
library, so it runs in any job that already has Python.

What travels, as one gzipped tar (``application/gzip``): ``bundle.tar.gz`` (the pytest bundle),
``neutral.tar.gz`` or a ``neutral/`` directory (the language-neutral bundle; for a Vitest run it is
all there is), ``meta.json``, and with ``--with-sources`` the measured source files under
``sources/<path>``. Sources are only ever files the neutral bundle's ``files`` table lists (plus
``pyproject.toml`` and the invariants file) **and** that ``git ls-files`` reports as tracked, so an
ignored ``.env`` or a generated secret can never ride along, whatever the bundle claims. The
archive is capped client-side (``--max-bytes``, default the server's default 200 MiB) before a byte
is sent; when the sources would push it over, it is sent without them. The body is streamed with
``Transfer-Encoding: chunked`` (a proxy that answers 411 gets a ``Content-Length`` retry), so the
server can enforce its own cap while reading instead of trusting a declared length.

Everything that names the run travels in headers (``X-Coverset-Repo``, ``-Sha``, ``-Base``,
``-Branch``, ``-Pr``, ``-Label``, ``-Shards``), so the server authorizes the request before it
reads the body. Authentication is ``Authorization: Bearer``: a ``cst_`` repository token with the
``upload`` scope (``--token``, else ``COVERSET_TOKEN``), or a GitHub Actions OIDC token the command
requests itself when ``ACTIONS_ID_TOKEN_REQUEST_URL``/``_TOKEN`` are set (audience: ``--audience``,
else the server URL, as the ``wait`` action does). The server is idempotent on (repository, commit,
label, archive sha256), so a retried upload is harmless and retries are safe.

In GitHub Actions the run's identity defaults from the environment: ``GITHUB_REPOSITORY``; for
``pull_request`` events the head SHA, base SHA and number from the event payload
(``GITHUB_EVENT_PATH``), never ``GITHUB_SHA``, which is the temporary merge commit; for ``push`` the
pushed commit and the ``before`` SHA as base; the branch from ``GITHUB_HEAD_REF`` (pull requests)
or ``GITHUB_REF_NAME``. Several shards of one commit (a matrix) upload separately with distinct
``--label`` values; the server merges them into one run.

Exit codes: 0 accepted (or, with ``--wait``, the run passed), 1 the run failed (``--wait``),
2 a usage or local problem (no bundle, no credentials, archive too large, timed out),
3 the server could not be reached or refused the upload, 5 no tests (``--wait``).
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, TextIO

from pytest_coverset.ci_http import NETWORK_ERRORS, USER_AGENT

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_SERVER = 3
EXIT_NO_TESTS = 5

DEFAULT_MAX_BYTES = 200 * 1024 * 1024
"""Client-side cap of the upload archive (the server's ``COVERSET_MAX_UPLOAD_BYTES`` default)."""
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
"""A larger measured file is left out of ``sources/`` (generated code, vendored data)."""
MAX_SOURCE_FILES = 4000
MAX_EVENT_BYTES = 16 * 1024 * 1024
"""``GITHUB_EVENT_PATH`` is read only up to this size (payloads are a few hundred KiB at most)."""
MAX_FILES_TABLE_BYTES = 64 * 1024 * 1024
LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
ZERO_SHA = "0" * 40
BUNDLE_MEMBERS = ("bundle.tar.gz", "neutral.tar.gz", "meta.json")
CONFIG_SOURCES = ("pyproject.toml", "invariants.yaml")
"""Tracked repository files sent with ``--with-sources`` besides the measured ones: the server reads
``[tool.coverset]`` and the invariants from them when it has no mirror of the repository."""
PR_EVENTS = frozenset({"pull_request", "pull_request_target"})
POLL_INTERVAL_S = 3.0
"""The longest pause between two looks at the run with ``--wait``."""
FIRST_POLL_S = 0.25


class UploadError(Exception):
    """A problem that ends the command with ``code``."""

    def __init__(self, message: str, code: int = EXIT_USAGE) -> None:
        super().__init__(message)
        self.code = code


# -- what the run is ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """Which commit (and pull request) the bundle belongs to."""

    repo: str
    sha: str
    base: str | None = None
    branch: str | None = None
    pr: int | None = None
    event: str | None = None
    title: str | None = None
    """The pull request's title (from the event payload), so a PR coverset first hears of through
    an upload is not shown as a bare number."""


def _read_event(environ: Mapping[str, str]) -> dict[str, Any]:
    """The Actions event payload (``GITHUB_EVENT_PATH``), or ``{}`` when absent or unreadable."""
    path = environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_EVENT_BYTES + 1)
    except OSError:
        return {}
    if len(data) > MAX_EVENT_BYTES:
        return {}
    try:
        payload = json.loads(data)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _sha_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and SHA_RE.fullmatch(value) and value != ZERO_SHA else None


def detect_identity(environ: Mapping[str, str]) -> dict[str, Any]:
    """Defaults for ``--repo/--sha/--base/--branch/--pr`` from a GitHub Actions environment.

    For ``pull_request`` events ``GITHUB_SHA`` is the merge commit GitHub made for the job, not a
    commit of the branch: the head SHA, base SHA and number come from the event payload instead.
    Outside Actions (no ``GITHUB_ACTIONS``) nothing is guessed.
    """
    if environ.get("GITHUB_ACTIONS", "").lower() != "true":
        return {}
    event_name = environ.get("GITHUB_EVENT_NAME") or None
    found: dict[str, Any] = {"repo": environ.get("GITHUB_REPOSITORY") or None, "event": event_name}
    payload = _read_event(environ)
    pull = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else None
    if event_name in PR_EVENTS and pull is not None:
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
        found["sha"] = _sha_or_none(head.get("sha"))
        found["base"] = _sha_or_none(base.get("sha"))
        number = pull.get("number", payload.get("number"))
        found["pr"] = number if isinstance(number, int) and not isinstance(number, bool) else None
        found["branch"] = environ.get("GITHUB_HEAD_REF") or head.get("ref") or None
        title = pull.get("title")
        found["title"] = title[:255] if isinstance(title, str) else None
        return found
    found["sha"] = _sha_or_none(environ.get("GITHUB_SHA"))
    if event_name == "push":
        found["base"] = _sha_or_none(payload.get("before"))
    ref = environ.get("GITHUB_REF", "")
    if ref.startswith("refs/heads/"):
        found["branch"] = environ.get("GITHUB_REF_NAME") or ref.removeprefix("refs/heads/")
    return found


def resolve_identity(args: argparse.Namespace, environ: Mapping[str, str]) -> RunIdentity:
    """Command-line values win over the Actions defaults; the result is validated."""
    detected = detect_identity(environ)
    repo = args.repo or detected.get("repo")
    sha = (args.sha or detected.get("sha") or "").lower()
    base = (args.base or detected.get("base") or "").lower() or None
    branch = args.branch or detected.get("branch")
    pr = args.pr if args.pr is not None else detected.get("pr")
    if not repo or not REPO_RE.fullmatch(repo):
        raise UploadError("--repo OWNER/NAME is required (GITHUB_REPOSITORY outside Actions)")
    if not SHA_RE.fullmatch(sha):
        raise UploadError("--sha must be a 40-hex commit SHA (the PR head, not a merge commit)")
    if base is not None and not SHA_RE.fullmatch(base):
        raise UploadError("--base must be a 40-hex commit SHA")
    if pr is not None and not 0 < int(pr) < 2**31:
        raise UploadError("--pr must be a positive pull request number")
    if branch is not None and (len(branch) > 255 or any(ord(c) < 0x20 or c == "\x7f" for c in branch)):
        raise UploadError("--branch is not a usable branch name")
    return RunIdentity(
        repo,
        sha,
        base,
        branch,
        int(pr) if pr is not None else None,
        detected.get("event"),
        detected.get("title") if pr == detected.get("pr") else None,
    )


def default_label(environ: Mapping[str, str]) -> str:
    """``COVERSET_LABEL``, else the Actions job id, else ``default`` (made label-safe)."""
    raw = environ.get("COVERSET_LABEL") or environ.get("GITHUB_JOB") or "default"
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw).strip("-._")[:64]
    return cleaned or "default"


# -- the archive --------------------------------------------------------------------------------


@dataclass(slots=True)
class Archive:
    """The upload archive on disk, with what went into it."""

    path: Path
    size: int
    sha256: str
    members: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    skipped_sources: dict[str, int] = field(default_factory=dict)


def _clean(info: tarfile.TarInfo) -> tarfile.TarInfo:
    """Reproducible, ownerless members: the same inputs give the same archive (and sha256)."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = 0
    info.mode = 0o644 if info.isfile() else 0o755
    return info


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = _clean(tarfile.TarInfo(name))
    info.size = len(data)
    tar.addfile(info, io.BytesIO(data))


def _regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _safe_rel(value: object) -> str | None:
    """A normalised relative POSIX path, else None (the server applies the same rule)."""
    if not isinstance(value, str) or not value or "\0" in value or "\\" in value or value.startswith("/"):
        return None
    if re.match(r"^[A-Za-z]:", value) or any(p in ("", ".", "..") for p in value.split("/")):
        return None
    return value


def measured_paths(bundle_dir: Path) -> list[str]:
    """The paths of the neutral bundle's ``files`` table (``neutral/files.jsonl.gz`` or inside
    ``neutral.tar.gz``); empty when the plugin wrote no neutral bundle."""
    raw: bytes | None = None
    direct = bundle_dir / "neutral" / "files.jsonl.gz"
    if _regular_file(direct):
        raw = direct.read_bytes()
    elif _regular_file(bundle_dir / "neutral.tar.gz"):
        try:
            with tarfile.open(bundle_dir / "neutral.tar.gz", "r:gz") as tar:
                for member in tar:
                    if member.isfile() and member.name.rstrip("/").endswith("files.jsonl.gz"):
                        extracted = tar.extractfile(member)
                        raw = extracted.read(MAX_FILES_TABLE_BYTES) if extracted is not None else None
                        break
        except (tarfile.TarError, OSError, EOFError):
            raw = None
    if raw is None:
        return []
    paths: list[str] = []
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as fh:
            for line in fh.read(MAX_FILES_TABLE_BYTES).splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                path = _safe_rel(row.get("path")) if isinstance(row, dict) else None
                if path is not None:
                    paths.append(path)
    except (OSError, EOFError):
        return []
    return sorted(set(paths))


def _git(args: Sequence[str], cwd: Path) -> bytes | None:
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, timeout=60, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def git_root(start: Path) -> Path | None:
    """The working tree root that contains ``start``, or None outside a git checkout."""
    out = _git(["rev-parse", "--show-toplevel"], start)
    if not out:
        return None
    return Path(out.decode("utf-8", "surrogateescape").strip())


def checkout_head(bundle_dir: Path) -> str | None:
    """The commit checked out where the bundle was measured (None outside git).

    ``actions/checkout`` checks out the temporary merge commit for ``pull_request`` events; the
    bundle's line numbers then belong to that commit, not to the PR head the upload names.
    """
    out = _git(["rev-parse", "HEAD"], _measured_root(bundle_dir, Path.cwd()))
    sha = out.decode().strip() if out else ""
    return sha if SHA_RE.fullmatch(sha) else None


def tracked(root: Path, paths: Iterable[str]) -> set[str]:
    """The subset of ``paths`` (relative to ``root``, the git top level) git tracks."""
    wanted = sorted(set(paths))
    if not wanted:
        return set()
    found: set[str] = set()
    for start in range(0, len(wanted), 500):
        chunk = wanted[start : start + 500]
        out = _git(["ls-files", "-z", "--full-name", "--", *(f":(literal){p}" for p in chunk)], root)
        if out is None:
            return set()
        found.update(name for name in out.decode("utf-8", "surrogateescape").split("\0") if name)
    return found & set(wanted)


def _measured_root(bundle_dir: Path, fallback: Path) -> Path:
    """Where the bundle's paths are relative to: ``meta.json``'s ``repo_root`` when it exists."""
    meta_path = bundle_dir / "meta.json"
    if _regular_file(meta_path):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        root = meta.get("repo_root") if isinstance(meta, dict) else None
        if isinstance(root, str) and root and Path(root).is_dir():
            return Path(root)
    return fallback


def _invariants_name(pyproject: Path) -> str:
    """``[tool.coverset] invariants`` from ``pyproject.toml`` (default ``invariants.yaml``)."""
    try:
        import tomllib

        with pyproject.open("rb") as fh:
            doc = tomllib.loads(fh.read(1024 * 1024).decode("utf-8"))
        tool = doc.get("tool", {})
        value = tool.get("coverset", {}).get("invariants") if isinstance(tool, dict) else None
    except (OSError, ValueError, AttributeError):
        return "invariants.yaml"
    return value if isinstance(value, str) and _safe_rel(value) is not None else "invariants.yaml"


def collect_sources(
    bundle_dir: Path, *, err: Callable[[str], None]
) -> tuple[Path, list[str], dict[str, int]]:
    """``(root, paths, skipped)``: the tracked measured files to send under ``sources/``.

    ``paths`` are relative to the bundle's repository root (the paths the bundle uses); ``skipped``
    counts what was left out and why (untracked, missing, too large, a symlink).
    """
    measured_root = _measured_root(bundle_dir, Path.cwd())
    top = git_root(measured_root)
    skipped: dict[str, int] = {}
    if top is None:
        err("coverset-ci: --with-sources needs a git checkout (only tracked files are sent); sending none")
        return measured_root, [], {"no_git_checkout": 1}
    candidates = measured_paths(bundle_dir)
    pyproject = measured_root / "pyproject.toml"
    extras = ["pyproject.toml", _invariants_name(pyproject)] if _regular_file(pyproject) else []
    extras = [name for name in extras if _regular_file(measured_root / name)]
    rel_prefix = os.path.relpath(measured_root.resolve(), top.resolve()).replace(os.sep, "/")
    prefix = "" if rel_prefix == "." else rel_prefix + "/"
    in_git = tracked(top, [prefix + p for p in [*candidates, *extras]])
    chosen: list[str] = []
    for path in dict.fromkeys([*candidates, *extras]):
        if prefix + path not in in_git:
            skipped["untracked"] = skipped.get("untracked", 0) + 1
            continue
        file = measured_root / path
        if file.is_symlink():
            skipped["symlink"] = skipped.get("symlink", 0) + 1
            continue
        if not _regular_file(file):
            skipped["missing"] = skipped.get("missing", 0) + 1
            continue
        if file.stat().st_size > MAX_SOURCE_FILE_BYTES:
            skipped["too_large"] = skipped.get("too_large", 0) + 1
            continue
        if len(chosen) >= MAX_SOURCE_FILES:
            skipped["too_many"] = skipped.get("too_many", 0) + 1
            continue
        chosen.append(path)
    return measured_root, chosen, skipped


def build_archive(
    bundle_dir: Path,
    out: Path,
    *,
    with_sources: bool,
    max_bytes: int,
    err: Callable[[str], None],
) -> Archive:
    """Pack the upload archive at ``out`` (see the module docstring); raise when nothing to send or
    when it would exceed ``max_bytes``."""
    if not bundle_dir.is_dir() or bundle_dir.is_symlink():
        raise UploadError(f"no bundle directory at {bundle_dir} (run `pytest --coverset` first)")
    present = [name for name in BUNDLE_MEMBERS if _regular_file(bundle_dir / name)]
    neutral_dir = bundle_dir / "neutral"
    has_neutral_dir = neutral_dir.is_dir() and not neutral_dir.is_symlink()
    if "bundle.tar.gz" not in present and "neutral.tar.gz" not in present and not has_neutral_dir:
        raise UploadError(
            f"{bundle_dir} holds neither bundle.tar.gz nor a neutral bundle (run `pytest --coverset`)"
        )
    archive = Archive(path=out, size=0, sha256="")
    # gzip with mtime 0 and ownerless, timeless members: the same files give the same bytes, so a
    # re-run of the upload step is recognised by the server as a replay (same sha256).
    with (
        out.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz,
        tarfile.open(fileobj=gz, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        for name in present:
            tar.add(bundle_dir / name, arcname=name, recursive=False, filter=_clean)
            archive.members.append(name)
        if has_neutral_dir and "neutral.tar.gz" not in present:
            for file in sorted(neutral_dir.iterdir()):
                if _regular_file(file):
                    tar.add(file, arcname=f"neutral/{file.name}", recursive=False, filter=_clean)
                    archive.members.append(f"neutral/{file.name}")
        if with_sources:
            root, paths, skipped = collect_sources(bundle_dir, err=err)
            archive.skipped_sources = skipped
            for path in paths:
                tar.add(root / path, arcname=f"sources/{path}", recursive=False, filter=_clean)
                archive.sources.append(path)
    archive.size = out.stat().st_size
    if archive.size > max_bytes and archive.sources:
        err(f"coverset-ci: with sources the archive is {archive.size} bytes, over the cap; sending none")
        out.unlink()
        return build_archive(bundle_dir, out, with_sources=False, max_bytes=max_bytes, err=err)
    if archive.size > max_bytes:
        raise UploadError(
            f"the upload archive is {archive.size} bytes, over the {max_bytes}-byte cap "
            "(--max-bytes; the server's COVERSET_MAX_UPLOAD_BYTES applies too)"
        )
    digest = hashlib.sha256()
    with out.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    archive.sha256 = digest.hexdigest()
    return archive


# -- credentials --------------------------------------------------------------------------------


def request_oidc_token(environ: Mapping[str, str], audience: str, *, timeout: float = 30.0) -> str:
    """Ask the Actions runtime for an OIDC token for ``audience`` (needs ``id-token: write``)."""
    url = environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise UploadError(
            "no OIDC token available: give the job `permissions: id-token: write`, or pass a "
            "repository token with --token / COVERSET_TOKEN"
        )
    separator = "&" if urllib.parse.urlsplit(url).query else "?"
    full = f"{url}{separator}{urllib.parse.urlencode({'audience': audience})}"
    request = urllib.request.Request(
        full,
        headers={"Authorization": f"Bearer {bearer}", "Accept": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read(1024 * 1024))
    except (*NETWORK_ERRORS, ValueError) as exc:
        raise UploadError(f"the OIDC token request failed: {type(exc).__name__}", EXIT_SERVER) from None
    value = body.get("value") if isinstance(body, dict) else None
    if not isinstance(value, str) or not value:
        raise UploadError("the OIDC token response carried no token", EXIT_SERVER)
    return value


@dataclass(slots=True)
class Credentials:
    """A bearer for the server; OIDC tokens are short-lived, so they can be requested again."""

    kind: str
    """``token`` or ``oidc``."""
    value: str
    refresh: Callable[[], str] | None = None

    def renew(self) -> bool:
        """Fetch a fresh OIDC token (False for a static token)."""
        if self.refresh is None:
            return False
        self.value = self.refresh()
        return True


def choose_credentials(args: argparse.Namespace, environ: Mapping[str, str]) -> Credentials:
    """``--token``/``COVERSET_TOKEN`` unless ``--oidc``; else OIDC when the Actions runtime offers it."""
    token = None if args.oidc else (args.token or environ.get("COVERSET_TOKEN") or None)
    if token:
        return Credentials("token", token.strip())
    audience = args.audience or environ.get("COVERSET_AUDIENCE") or args.server.rstrip("/")

    def fetch() -> str:
        return request_oidc_token(environ, audience)

    if args.oidc or (
        environ.get("ACTIONS_ID_TOKEN_REQUEST_URL") and environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    ):
        return Credentials("oidc", fetch(), refresh=fetch)
    raise UploadError(
        "no credentials: pass a repository token (--token or COVERSET_TOKEN, scope upload) or run in "
        "GitHub Actions with `permissions: id-token: write`"
    )


# -- HTTP -----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: Any
    headers: Mapping[str, str]


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


class Uploader:
    """POSTs the archive and polls the run, with bounded retries until ``deadline``."""

    def __init__(
        self,
        server: str,
        credentials: Credentials,
        *,
        deadline: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 5,
    ) -> None:
        self.server = server.rstrip("/")
        self.credentials = credentials
        self.deadline = deadline
        self.clock = clock
        self.sleep = sleep
        self.max_retries = max_retries

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def _send(
        self, method: str, path: str, headers: Mapping[str, str], body: BinaryIO | None = None
    ) -> Answer:
        all_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "Authorization": f"Bearer {self.credentials.value}",
            **headers,
        }
        request = urllib.request.Request(
            f"{self.server}{path}", data=body, headers=all_headers, method=method
        )
        timeout = max(1.0, min(120.0, self.remaining()))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return Answer(response.status, _decode(response.read(1024 * 1024)), dict(response.headers))
        except urllib.error.HTTPError as exc:
            return Answer(exc.code, _decode(exc.read(1024 * 1024)), dict(exc.headers or {}))

    def call(
        self, method: str, path: str, headers: Mapping[str, str], body_path: Path | None = None
    ) -> Answer:
        """One request with retries on network errors, 5xx and 429, and one OIDC renewal on 401.

        A connection the server drops while the archive is still being sent is tried once more,
        then reported as what it almost always is: the server's size cap answering early (a 413
        the client never gets to read). Retrying it as an outage would only resend the archive
        until the backoff runs out.
        """
        failures = 0
        dropped = 0
        renewed = False
        while True:
            try:
                if body_path is not None:
                    with body_path.open("rb") as fh:
                        answer = self._send(method, path, headers, fh)
                else:
                    answer = self._send(method, path, headers)
                error = None
            except NETWORK_ERRORS as exc:
                answer, error = None, f"{type(exc).__name__}: {exc}"
                if body_path is not None and _connection_dropped(exc):
                    dropped += 1
                    if dropped > 1:
                        size = body_path.stat().st_size
                        raise UploadError(
                            f"the server closed the connection while receiving the archive ({size} "
                            "bytes): it is probably over the server's size cap "
                            f"(COVERSET_MAX_UPLOAD_BYTES); last error {error}",
                            EXIT_SERVER,
                        ) from None
            if answer is not None:
                if answer.status == 401 and not renewed and self.credentials.renew():
                    renewed = True
                    continue
                if answer.status < 500 and answer.status != 429:
                    return answer
                error = f"HTTP {answer.status}"
            failures += 1
            delay = min(30.0, 2.0 ** (failures - 1))
            if answer is not None and answer.status == 429:
                delay = _retry_after(answer.headers, delay)
            if failures > self.max_retries or delay >= self.remaining():
                raise UploadError(f"the server is unavailable ({error}, {failures} attempts)", EXIT_SERVER)
            self.sleep(delay)


DROPPED = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
"""The server went away mid-request: a pipe broken while sending, or a reset/``RemoteDisconnected``
(a ``ConnectionResetError``) when it closed before answering. ``urlopen`` wraps the first kind in
``URLError``; the second escapes ``getresponse`` as it is."""


def _connection_dropped(exc: BaseException) -> bool:
    """Whether ``exc`` (raw or wrapped in ``URLError``) says the server closed the connection."""
    if isinstance(exc, DROPPED):
        return True
    reason = getattr(exc, "reason", None)
    return isinstance(reason, DROPPED)


def _retry_after(headers: Mapping[str, str], default: float) -> float:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        return float(value) if value is not None else default
    except ValueError:
        return default


def upload_headers(identity: RunIdentity, archive: Archive, label: str, shards: int | None) -> dict[str, str]:
    """The request headers that name the run (the body is only the archive)."""
    headers = {
        "Content-Type": "application/gzip",
        "X-Coverset-Repo": identity.repo,
        "X-Coverset-Sha": identity.sha,
        "X-Coverset-Label": label,
        "X-Coverset-Archive-Sha256": archive.sha256,
    }
    if identity.base:
        headers["X-Coverset-Base"] = identity.base
    if identity.branch:
        headers["X-Coverset-Branch"] = urllib.parse.quote(identity.branch, safe="/._-")
    if identity.pr is not None:
        headers["X-Coverset-Pr"] = str(identity.pr)
    if identity.title:
        headers["X-Coverset-Pr-Title"] = urllib.parse.quote(identity.title, safe="")
    if shards is not None:
        headers["X-Coverset-Shards"] = str(shards)
    return headers


def exit_code_for(conclusion: str | None, exit_code: object, counts: Mapping[str, Any] | None) -> int:
    """The process exit code for a finished run (the ``wait`` command's rules)."""
    from pytest_coverset.ci import exit_code_for as wait_exit_code

    return wait_exit_code(
        conclusion,
        exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        {k: v for k, v in (counts or {}).items() if isinstance(v, int)} if counts is not None else None,
    )


def follow(uploader: Uploader, run_id: str, out: Callable[[str], None], *, poll_s: float) -> int:
    """Poll ``GET /v1/bundles/<run_id>`` until the run is finished; print its result.

    Polls start at :data:`FIRST_POLL_S` and double up to ``poll_s``: a small run's verdict is
    usually ready within a second of the upload, a large one's later.
    """
    announced = False
    delay = min(FIRST_POLL_S, poll_s)
    while True:
        answer = uploader.call("GET", f"/v1/bundles/{urllib.parse.quote(run_id, safe='')}", {})
        body = answer.body if isinstance(answer.body, dict) else {}
        if answer.status != 200:
            raise UploadError(
                f"the run lookup answered HTTP {answer.status}: {body.get('error', '')}", EXIT_SERVER
            )
        if body.get("state") == "completed":
            counts = body.get("counts") if isinstance(body.get("counts"), dict) else None
            ran = (
                ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in (counts or {}).items() if v) or "no tests"
            )
            out(f"coverset verdict: {body.get('conclusion')} ({ran})")
            if body.get("summary"):
                out(f"  {body['summary']}")
            if body.get("patch_pct") is not None:
                out(f"  patch coverage {body['patch_pct']}%")
            for failure in body.get("failures") or []:
                if isinstance(failure, dict):
                    out(f"  FAILED {failure.get('nodeid', '?')}: {failure.get('message', '')}")
            if body.get("summary_url"):
                out(f"  details: {body['summary_url']}")
            return exit_code_for(body.get("conclusion"), body.get("exit_code"), counts)
        if not announced:
            out(f"coverset-ci: waiting for run {run_id} ({body.get('state', '?')})")
            announced = True
        if uploader.remaining() <= delay:
            raise UploadError(f"timed out waiting for run {run_id}", EXIT_USAGE)
        uploader.sleep(delay)
        delay = min(poll_s, delay * 2)


# -- command --------------------------------------------------------------------------------------


def add_parser(commands: Any) -> argparse.ArgumentParser:
    """Register ``upload`` on the ``coverset-ci`` sub-command parser."""
    upload = commands.add_parser(
        "upload", help="send the bundle `pytest --coverset` wrote to coverset (upload mode)"
    )
    upload.add_argument(
        "--server", required=True, help="coverset base URL, e.g. https://coverset.example.com"
    )
    upload.add_argument("--repo", help="OWNER/NAME (default GITHUB_REPOSITORY)")
    upload.add_argument("--sha", help="40-hex commit tested (default: the PR head or the pushed commit)")
    upload.add_argument("--base", help="40-hex base commit for patch coverage (default: from the event)")
    upload.add_argument("--branch", help="branch name (default GITHUB_HEAD_REF or GITHUB_REF_NAME)")
    upload.add_argument("--pr", type=int, help="pull request number (default: from the event)")
    upload.add_argument(
        "--dir", default=".coverset", help="the plugin's output directory (default .coverset)"
    )
    upload.add_argument("--token", help="cst_ repository token with the upload scope (else COVERSET_TOKEN)")
    upload.add_argument(
        "--oidc", action="store_true", help="use a GitHub Actions OIDC token even if a token is set"
    )
    upload.add_argument("--audience", help="OIDC audience (default COVERSET_AUDIENCE, else the server URL)")
    upload.add_argument(
        "--with-sources", action="store_true", help="also send the measured, git-tracked source files"
    )
    upload.add_argument(
        "--label",
        help="shard name; one per matrix leg (default COVERSET_LABEL, else GITHUB_JOB, which is the "
        "same for every leg of a matrix job)",
    )
    upload.add_argument(
        "--shards",
        type=int,
        help="how many labels this commit uploads (the verdict waits for all); needs an explicit label",
    )
    upload.add_argument("--timeout", type=float, default=300.0, help="give up after this many seconds")
    upload.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="client-side archive cap")
    upload.add_argument("--wait", action="store_true", help="follow the run and exit with its verdict")
    return upload


def run_upload(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
    out: TextIO,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run ``coverset-ci upload``; returns the exit code."""
    from pytest_coverset.ci import printable

    env = os.environ if environ is None else environ

    def say(text: str) -> None:
        out.write(printable(text) + "\n")
        out.flush()

    try:
        label = args.label or default_label(env)
        if not LABEL_RE.fullmatch(label):
            raise UploadError("--label: 1-64 characters of A-Z a-z 0-9 . _ - (starting alphanumeric)")
        if args.shards is not None and not 0 < args.shards <= 256:
            raise UploadError("--shards must be between 1 and 256")
        if args.shards is not None and args.shards > 1 and not (args.label or env.get("COVERSET_LABEL")):
            # GITHUB_JOB is the job id, identical for every leg of a matrix job: the legs would
            # replace each other's shard and the run would wait for a label that never comes.
            raise UploadError(
                "--shards needs an explicit --label (or COVERSET_LABEL) per matrix job: the default "
                "label, GITHUB_JOB, is the same for every leg of a matrix"
            )
        if not args.server.startswith(("https://", "http://")):
            raise UploadError("--server must be an http(s) URL")
        identity = resolve_identity(args, env)
        checked_out = checkout_head(Path(args.dir))
        if checked_out is not None and checked_out != identity.sha:
            say(
                f"coverset-ci: warning: the tests ran on {checked_out[:12]}, not {identity.sha[:12]}; "
                "for pull requests check out the head (actions/checkout `ref: "
                "${{ github.event.pull_request.head.sha }}`) so line numbers match the PR"
            )
        credentials = choose_credentials(args, env)
        with tempfile.TemporaryDirectory(prefix="coverset-upload-") as tmp:
            archive = build_archive(
                Path(args.dir),
                Path(tmp) / "upload.tar.gz",
                with_sources=args.with_sources,
                max_bytes=args.max_bytes,
                err=say,
            )
            sources = f", {len(archive.sources)} source files" if args.with_sources else ""
            say(
                f"coverset-ci: uploading {archive.size} bytes for {identity.repo}@{identity.sha[:12]}"
                f" (label {label}{sources})"
            )
            if archive.skipped_sources:
                skipped = ", ".join(
                    f"{n} {why.replace('_', ' ')}" for why, n in sorted(archive.skipped_sources.items())
                )
                say(f"coverset-ci: sources left out: {skipped}")
            uploader = Uploader(
                args.server, credentials, deadline=clock() + args.timeout, clock=clock, sleep=sleep
            )
            headers = upload_headers(identity, archive, label, args.shards)
            answer = uploader.call("POST", "/v1/bundles", headers, archive.path)
            if answer.status == 411:  # a proxy that refuses chunked bodies: say the length up front
                headers["Content-Length"] = str(archive.size)
                answer = uploader.call("POST", "/v1/bundles", headers, archive.path)
        body = answer.body if isinstance(answer.body, dict) else {}
        if answer.status not in (200, 202):
            detail = body.get("detail") or body.get("error") or ""
            raise UploadError(f"upload refused: HTTP {answer.status} {detail}".rstrip(), EXIT_SERVER)
        run_id = str(body.get("run_id", ""))
        replay = " (already uploaded)" if body.get("duplicate") else ""
        say(f"coverset run {run_id}: {body.get('state', '?')}{replay}")
        if body.get("url"):
            say(f"details: {body['url']}")
        if not args.wait:
            return EXIT_OK
        return follow(uploader, run_id, say, poll_s=POLL_INTERVAL_S)
    except UploadError as exc:
        say(f"coverset-ci: {exc}")
        return exc.code
