"""``coverset-ci upload`` against a local HTTP stub: identity from Actions, credentials, the archive.

The stub plays the coverset server (``POST /v1/bundles``, ``GET /v1/bundles/<id>``) and the Actions
OIDC token endpoint, decodes chunked bodies the way Hypercorn does, and records every request, so
the tests can check exactly what the command sends: the headers that name the run (the PR head SHA
from the event payload, never the merge commit), the bearer, and the archive's members (only
git-tracked measured files under ``sources/``).
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import socket
import subprocess
import tarfile
import threading
import time
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from pytest_coverset.ci import main
from pytest_coverset.ci_upload import detect_identity, measured_paths

HEAD = "a" * 40
BASE = "b" * 40
MERGE = "c" * 40
TOKEN = "cst_abcdefgh_" + "s" * 43
DROP = 0
"""A ``post_status`` entry that makes the stub drop the connection instead of answering."""


@dataclass
class Stub:
    """What the stub answers and what it saw."""

    post_status: list[int] = field(default_factory=lambda: [202])
    status_bodies: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    oidc_value: str = "oidc-jwt-value"
    url: str = ""
    lock: threading.Lock = field(default_factory=threading.Lock)

    def next_post(self) -> int:
        with self.lock:
            return self.post_status.pop(0) if len(self.post_status) > 1 else self.post_status[0]

    def posts(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["method"] == "POST"]


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _body(self) -> bytes:
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            data = bytearray()
            while True:
                size = int(self.rfile.readline().strip().split(b";")[0], 16)
                if size == 0:
                    self.rfile.readline()
                    return bytes(data)
                data.extend(self.rfile.read(size))
                self.rfile.readline()
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length)

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _record(self, body: bytes = b"") -> None:
        self.server.stub.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        stub = self.server.stub
        self._record()
        if self.path.startswith("/token"):
            self._json(200, {"value": stub.oidc_value})
        elif self.path.startswith("/v1/bundles/"):
            body = stub.status_bodies.pop(0) if len(stub.status_bodies) > 1 else stub.status_bodies[0]
            self._json(200, body)
        else:
            self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        stub = self.server.stub
        status = stub.next_post()
        if status == DROP:  # the size cap tripping mid-body: close without reading or answering
            self._record()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        if status == 411 and self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            self._body()  # drained, so the client reads the answer instead of a reset connection
            self._record()
            self._json(411, {"error": "length_required"})
            self.close_connection = True
            return
        self._record(self._body())
        if status >= 300 and status != 411:
            self._json(status, {"error": "refused", "detail": "nope"})
            return
        self._json(
            202, {"run_id": "run_1", "url": f"{stub.url}/runs/run_1", "state": "running", "duplicate": False}
        )


class _Server(ThreadingHTTPServer):
    stub: Stub


@pytest.fixture
def stub() -> Iterator[Stub]:
    state = Stub()
    server = _Server(("127.0.0.1", 0), _Handler)
    server.stub = state
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()


def _files_jsonl(paths: list[str]) -> bytes:
    rows = "".join(json.dumps({"path": p, "language": "python"}) + "\n" for p in paths)
    return gzip.compress(rows.encode())


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    """A ``.coverset`` directory like the plugin's (contents are opaque to the client)."""
    out = tmp_path / "repo" / ".coverset"
    (out / "neutral").mkdir(parents=True)
    (out / "bundle.tar.gz").write_bytes(b"python-bundle")
    (out / "meta.json").write_text(json.dumps({"repo_root": str(tmp_path / "repo")}))
    (out / "neutral" / "manifest.json").write_text("{}")
    (out / "neutral" / "files.jsonl.gz").write_bytes(_files_jsonl(["pkg/core.py", "pkg/secret.py"]))
    (out / "coverage.sqlite").write_bytes(b"not sent")
    return out


def _members(body: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar if m.isfile()}  # type: ignore[union-attr]


def _run(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, env: dict[str, str] | None = None
) -> tuple[int, str]:
    for name in list(__import__("os").environ):
        if name.startswith(("GITHUB_", "ACTIONS_", "COVERSET")):
            monkeypatch.delenv(name, raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    out = io.StringIO()
    code = main(argv, out=out)
    return code, out.getvalue()


def _upload_args(stub: Stub, bundle: Path, *extra: str) -> list[str]:
    return ["upload", "--server", stub.url, "--dir", str(bundle), "--timeout", "20", *extra]


def test_a_token_upload_sends_the_run_in_headers_and_the_bundle_as_a_chunked_tarball(
    stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = _run(
        _upload_args(
            stub,
            bundle,
            "--repo",
            "acme/widgets",
            "--sha",
            HEAD,
            "--base",
            BASE,
            "--pr",
            "4",
            "--branch",
            "feature/x",
            "--label",
            "py313",
        ),
        monkeypatch,
        {"COVERSET_TOKEN": TOKEN},
    )
    assert code == 0, out
    assert f"details: {stub.url}/runs/run_1" in out
    assert TOKEN not in out
    (post,) = stub.posts()
    headers = post["headers"]
    assert headers["authorization"] == f"Bearer {TOKEN}"
    assert headers["transfer-encoding"] == "chunked" and "content-length" not in headers
    assert (headers["x-coverset-repo"], headers["x-coverset-sha"], headers["x-coverset-base"]) == (
        "acme/widgets",
        HEAD,
        BASE,
    )
    assert (headers["x-coverset-pr"], headers["x-coverset-label"]) == ("4", "py313")
    assert urllib.parse.unquote(headers["x-coverset-branch"]) == "feature/x"
    assert headers["x-coverset-archive-sha256"] == hashlib.sha256(post["body"]).hexdigest()
    members = _members(post["body"])
    assert set(members) == {"bundle.tar.gz", "meta.json", "neutral/manifest.json", "neutral/files.jsonl.gz"}
    assert members["bundle.tar.gz"] == b"python-bundle"


def test_the_archive_is_reproducible(stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = _upload_args(stub, bundle, "--repo", "acme/widgets", "--sha", HEAD)
    for attempt in range(2):
        if attempt:
            time.sleep(1.1)  # gzip headers carry a timestamp in seconds unless it is pinned
        assert _run(args, monkeypatch, {"COVERSET_TOKEN": TOKEN})[0] == 0
    first, second = stub.posts()
    assert first["headers"]["x-coverset-archive-sha256"] == second["headers"]["x-coverset-archive-sha256"]


def _actions_env(
    tmp_path: Path, stub: Stub, event: str, payload: dict[str, Any], **extra: str
) -> dict[str, str]:
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "acme/widgets",
        "GITHUB_EVENT_NAME": event,
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_SHA": MERGE,
        "GITHUB_JOB": "tests",
        "ACTIONS_ID_TOKEN_REQUEST_URL": f"{stub.url}/token?api-version=2.0",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "runtime-token",
    }
    env.update(extra)
    return env


PR_PAYLOAD = {
    "number": 12,
    "pull_request": {
        "number": 12,
        "title": "Make it faster",
        "head": {"sha": HEAD, "ref": "faster"},
        "base": {"sha": BASE, "ref": "main"},
    },
}


def test_in_actions_a_pull_request_uploads_its_head_not_the_merge_commit_with_oidc(
    stub: Stub, bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _actions_env(tmp_path, stub, "pull_request", PR_PAYLOAD, GITHUB_HEAD_REF="faster")
    code, out = _run(_upload_args(stub, bundle), monkeypatch, env)
    assert code == 0, out
    token_request = next(r for r in stub.requests if r["path"].startswith("/token"))
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(token_request["path"]).query)
    assert query == {"api-version": ["2.0"], "audience": [stub.url]}
    assert token_request["headers"]["authorization"] == "Bearer runtime-token"
    headers = stub.posts()[0]["headers"]
    assert headers["authorization"] == "Bearer oidc-jwt-value"
    assert (headers["x-coverset-sha"], headers["x-coverset-base"], headers["x-coverset-pr"]) == (
        HEAD,
        BASE,
        "12",
    )
    assert headers["x-coverset-label"] == "tests"
    assert urllib.parse.unquote(headers["x-coverset-pr-title"]) == "Make it faster"
    assert urllib.parse.unquote(headers["x-coverset-branch"]) == "faster"


def test_a_token_wins_over_oidc_unless_oidc_is_asked_for(
    stub: Stub, bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = _actions_env(tmp_path, stub, "pull_request", PR_PAYLOAD, COVERSET_TOKEN=TOKEN)
    assert _run(_upload_args(stub, bundle), monkeypatch, env)[0] == 0
    assert stub.posts()[-1]["headers"]["authorization"] == f"Bearer {TOKEN}"
    assert (
        _run(_upload_args(stub, bundle, "--oidc", "--audience", "https://aud.example"), monkeypatch, env)[0]
        == 0
    )
    assert stub.posts()[-1]["headers"]["authorization"] == "Bearer oidc-jwt-value"
    token_request = [r for r in stub.requests if r["path"].startswith("/token")][-1]
    assert "audience=https%3A%2F%2Faud.example" in token_request["path"]


def test_push_events_use_the_pushed_commit_and_before_as_base() -> None:
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "acme/widgets",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_SHA": HEAD,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_NAME": "main",
    }
    assert detect_identity(env) | {} == {
        "repo": "acme/widgets",
        "event": "push",
        "sha": HEAD,
        "branch": "main",
        "base": None,
    }


def test_push_identity_reads_before_from_the_payload(tmp_path: Path) -> None:
    event = tmp_path / "push.json"
    event.write_text(json.dumps({"before": BASE}))
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_SHA": HEAD,
        "GITHUB_EVENT_PATH": str(event),
    }
    assert detect_identity(env)["base"] == BASE
    event.write_text(json.dumps({"before": "0" * 40}))
    assert detect_identity(env)["base"] is None


def test_outside_actions_nothing_is_guessed(
    stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert detect_identity({"GITHUB_SHA": HEAD, "GITHUB_REPOSITORY": "a/b"}) == {}
    code, out = _run(_upload_args(stub, bundle), monkeypatch, {"COVERSET_TOKEN": TOKEN})
    assert code == 2 and "--repo OWNER/NAME is required" in out


def test_no_credentials_and_no_bundle_are_usage_errors(
    stub: Stub, bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = _run(_upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD), monkeypatch)
    assert code == 2 and "no credentials" in out
    empty = tmp_path / "empty"
    empty.mkdir()
    code, out = _run(
        _upload_args(stub, empty, "--repo", "a/b", "--sha", HEAD), monkeypatch, {"COVERSET_TOKEN": TOKEN}
    )
    assert code == 2 and "neither bundle.tar.gz nor a neutral bundle" in out
    assert stub.posts() == []


def test_the_client_side_cap_refuses_before_sending(
    stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, out = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD, "--max-bytes", "64"),
        monkeypatch,
        {"COVERSET_TOKEN": TOKEN},
    )
    assert code == 2 and "over the 64-byte cap" in out
    assert stub.posts() == []


def test_a_refusal_exits_3_and_server_errors_are_retried(
    stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.post_status = [403]
    code, out = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD), monkeypatch, {"COVERSET_TOKEN": TOKEN}
    )
    assert code == 3 and "upload refused: HTTP 403 nope" in out
    stub.post_status = [503, 202]
    stub.requests.clear()
    monkeypatch.setattr("pytest_coverset.ci_upload.time.sleep", lambda s: None)
    code, out = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD), monkeypatch, {"COVERSET_TOKEN": TOKEN}
    )
    assert code == 0, out
    assert len(stub.posts()) == 2


def test_a_dropped_connection_is_tried_once_more_then_blamed_on_the_size_cap(
    stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server that closes mid-body (its cap tripped) must not become a 31 s retry storm."""
    stub.post_status = [DROP]
    started = time.monotonic()
    code, out = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD), monkeypatch, {"COVERSET_TOKEN": TOKEN}
    )
    assert code == 3, out
    assert "closed the connection while receiving the archive" in out and "COVERSET_MAX_UPLOAD_BYTES" in out
    assert len(stub.posts()) == 2 and time.monotonic() - started < 10  # one 1 s backoff, not 31 s
    # One drop followed by an answer is still a retry, like any other network blip.
    stub.post_status = [DROP, 202]
    stub.requests.clear()
    code, out = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD), monkeypatch, {"COVERSET_TOKEN": TOKEN}
    )
    assert code == 0, out


def test_shards_without_an_explicit_label_is_refused(
    stub: Stub, bundle: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GITHUB_JOB is the same for every leg of a matrix job: with ``--shards`` it cannot be the label."""
    env = _actions_env(tmp_path, stub, "pull_request", PR_PAYLOAD, COVERSET_TOKEN=TOKEN)
    code, out = _run(_upload_args(stub, bundle, "--shards", "2"), monkeypatch, env)
    assert code == 2 and "GITHUB_JOB" in out and stub.posts() == []
    assert _run(_upload_args(stub, bundle, "--shards", "2", "--label", "py313"), monkeypatch, env)[0] == 0
    assert stub.posts()[-1]["headers"]["x-coverset-label"] == "py313"
    env["COVERSET_LABEL"] = "py312"
    assert _run(_upload_args(stub, bundle, "--shards", "2"), monkeypatch, env)[0] == 0
    assert stub.posts()[-1]["headers"]["x-coverset-label"] == "py312"
    assert (
        _run(_upload_args(stub, bundle, "--shards", "1"), monkeypatch, {**env, "COVERSET_LABEL": ""})[0] == 0
    )


def test_a_proxy_that_refuses_chunked_bodies_gets_a_content_length(
    stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub.post_status = [411, 202]
    code, out = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD), monkeypatch, {"COVERSET_TOKEN": TOKEN}
    )
    assert code == 0, out
    retried = stub.posts()[-1]
    assert int(retried["headers"]["content-length"]) == len(retried["body"]) > 0


def test_wait_follows_the_run_and_exits_with_its_code(
    stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pytest_coverset.ci_upload.POLL_INTERVAL_S", 0.01)
    stub.status_bodies = [
        {"run_id": "run_1", "state": "running"},
        {
            "run_id": "run_1",
            "state": "completed",
            "conclusion": "failure",
            "exit_code": 1,
            "counts": {"passed": 3, "failed": 1},
            "summary": "1 failure",
            "patch_pct": 87.5,
            "failures": [{"nodeid": "tests/test_a.py::test_x", "message": "assert 1 == 2"}],
            "summary_url": f"{stub.url}/runs/run_1",
        },
    ]
    code, out = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD, "--wait"),
        monkeypatch,
        {"COVERSET_TOKEN": TOKEN},
    )
    assert code == 1, out
    assert "coverset verdict: failure (3 passed, 1 failed)" in out
    assert "patch coverage 87.5%" in out
    assert "FAILED tests/test_a.py::test_x: assert 1 == 2" in out
    status_requests = [r for r in stub.requests if r["path"] == "/v1/bundles/run_1"]
    assert len(status_requests) == 2
    assert all(r["headers"]["authorization"] == f"Bearer {TOKEN}" for r in status_requests)


def test_wait_exits_5_when_no_tests_ran(stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stub.status_bodies = [
        {"run_id": "run_1", "state": "completed", "conclusion": "neutral", "exit_code": 5, "counts": {}}
    ]
    code, _ = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD, "--wait"),
        monkeypatch,
        {"COVERSET_TOKEN": TOKEN},
    )
    assert code == 5


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_with_sources_sends_only_tracked_measured_files(
    stub: Stub, bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = bundle.parent
    (repo / "pkg").mkdir()
    (repo / "pkg" / "core.py").write_text("def f():\n    return 1\n")
    (repo / "pkg" / "secret.py").write_text("TOKEN = 'never'\n")
    (repo / "pyproject.toml").write_text('[tool.coverset]\nsource = ["pkg"]\n')
    (repo / ".gitignore").write_text("pkg/secret.py\n.coverset/\n")
    _git(repo, "init", "-q")
    _git(repo, "add", "pkg/core.py", "pyproject.toml", ".gitignore")
    _git(repo, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
    code, out = _run(
        _upload_args(stub, bundle, "--repo", "a/b", "--sha", HEAD, "--with-sources"),
        monkeypatch,
        {"COVERSET_TOKEN": TOKEN},
    )
    assert code == 0, out
    members = _members(stub.posts()[0]["body"])
    sources = sorted(name for name in members if name.startswith("sources/"))
    assert sources == ["sources/pkg/core.py", "sources/pyproject.toml"]
    assert "sources left out: 1 untracked" in out
    # The checkout is not the commit named by --sha: the upload goes ahead with a warning.
    assert "warning: the tests ran on" in out


def test_measured_paths_drop_unsafe_entries(tmp_path: Path) -> None:
    (tmp_path / "neutral").mkdir()
    (tmp_path / "neutral" / "files.jsonl.gz").write_bytes(
        _files_jsonl(["ok/a.py", "../escape.py", "/abs.py", "a/./b.py", "ok/a.py"])
    )
    assert measured_paths(tmp_path) == ["ok/a.py"]


def test_measured_paths_read_a_plain_files_table(tmp_path: Path) -> None:
    """Vitest's reporter writes ``neutral/files.jsonl`` uncompressed; the gzipped name wins when both exist."""
    (tmp_path / "neutral").mkdir()
    rows = "".join(json.dumps({"path": p}) + "\n" for p in ["src/a.ts", "src/b.ts", "../x.ts"])
    (tmp_path / "neutral" / "files.jsonl").write_text(rows)
    assert measured_paths(tmp_path) == ["src/a.ts", "src/b.ts"]
    (tmp_path / "neutral" / "files.jsonl.gz").write_bytes(_files_jsonl(["pkg/a.py"]))
    assert measured_paths(tmp_path) == ["pkg/a.py"]


@pytest.mark.parametrize("name", ["files.jsonl", "files.jsonl.gz"])
def test_measured_paths_read_the_table_inside_neutral_tar_gz(tmp_path: Path, name: str) -> None:
    rows = "".join(json.dumps({"path": p}) + "\n" for p in ["lib/x.rs", "lib/y.rs"]).encode()
    data = gzip.compress(rows) if name.endswith(".gz") else rows
    with tarfile.open(tmp_path / "neutral.tar.gz", "w:gz") as tar:
        info = tarfile.TarInfo(f"neutral/{name}")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    assert measured_paths(tmp_path) == ["lib/x.rs", "lib/y.rs"]


def _monorepo(tmp_path: Path) -> tuple[Path, Path]:
    """A git checkout whose project (and ``.coverset``) lives in ``web/app``; paths are relative to it."""
    top = tmp_path / "mono"
    project = top / "web" / "app"
    (project / "src").mkdir(parents=True)
    (project / "src" / "a.ts").write_text("export const a = 1\n")
    (project / "src" / "untracked.ts").write_text("export const b = 2\n")
    (top / "README.md").write_text("mono\n")
    _git(top, "init", "-q")
    _git(top, "add", "README.md", "web/app/src/a.ts")
    _git(top, "-c", "user.email=t@example.com", "-c", "user.name=t", "commit", "-q", "-m", "init")
    bundle = project / ".coverset"
    (bundle / "neutral").mkdir(parents=True)
    rows = "".join(json.dumps({"path": p}) + "\n" for p in ["src/a.ts", "src/untracked.ts"])
    (bundle / "neutral" / "files.jsonl").write_text(rows)
    return project, bundle


@pytest.mark.parametrize("root", ["absolute", "relative"])
def test_with_sources_resolves_paths_against_the_recorded_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    """A project in a subdirectory of the checkout: the bundle's paths are relative to the project,
    ``meta.json`` says where that is, and the upload runs from somewhere else entirely."""
    from pytest_coverset.ci_upload import collect_sources

    project, bundle = _monorepo(tmp_path)
    recorded = str(project) if root == "absolute" else "."
    (bundle / "meta.json").write_text(json.dumps({"repo_root": recorded}))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    errors: list[str] = []
    found_root, paths, skipped = collect_sources(bundle, err=errors.append)
    assert found_root.resolve() == project.resolve()
    assert paths == ["src/a.ts"] and skipped == {"untracked": 1} and errors == []


def test_without_a_recorded_root_paths_resolve_against_the_current_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pytest_coverset.ci_upload import collect_sources

    project, bundle = _monorepo(tmp_path)
    monkeypatch.chdir(project)
    root, paths, _ = collect_sources(bundle, err=lambda message: None)
    assert root.resolve() == project.resolve() and paths == ["src/a.ts"]
    monkeypatch.chdir(project.parent.parent)  # the checkout's top: src/a.ts is not there
    _, paths, skipped = collect_sources(bundle, err=lambda message: None)
    assert paths == [] and skipped == {"untracked": 2}
