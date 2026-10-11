"""Repositories and their bare mirrors: the only place the control plane runs git against a source.

Every repo has a bare mirror at ``<data_dir>/mirrors/<repo_id>.git``. For a local source (a bare or
working repository on disk) the mirror is a ``git clone --mirror`` of it, refreshed with ``git fetch
--prune``. For a forge repository the mirror is fetched over HTTPS from the forge's canonical URL
(never a URL from a webhook payload) with a short-lived token passed only as ``-c
http.extraHeader=Authorization: ...`` on the command line: it is never written into a URL, the
mirror's ``config`` or a log line. Workers never see the mirror: they get ``git archive`` tarballs.

Local repositories are registered with ``forge='local'`` and ``external_id`` = sha256 of the
resolved source path, so registering the same path twice finds the same row.

The git helpers here are synchronous (callers in async code wrap them in ``asyncio.to_thread``) and
run git with a scrubbed environment that ignores user and system configuration.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coverset.analysis.diff import GitDiffError, validate_ref
from coverset.config import Settings
from coverset.db.models import Repo

if TYPE_CHECKING:
    from coverset.forge.base import Forge, MirrorCredentials

__all__ = [
    "RepoError",
    "archive",
    "default_branch_sha",
    "ensure_mirror",
    "ensure_mirror_async",
    "extract_archive",
    "git_env",
    "local_external_id",
    "merge_base",
    "mirror_path",
    "read_blob",
    "read_blobs",
    "register_local_repo",
    "resolve_commit",
    "tree_paths",
]

GIT_TIMEOUT_S = 300
FORGE_GIT_HOSTS = {"github": "https://github.com"}
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class RepoError(RuntimeError):
    """A git operation on a source or mirror failed."""


def git_env() -> dict[str, str]:
    """Environment for git: no inherited ``GIT_*``, no user/system config, never prompt."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="true",
        LC_ALL="C",
    )
    return env


def _git(args: list[str], *, timeout: float = GIT_TIMEOUT_S, secret_args: int = 0) -> bytes:
    """Run ``git <args>``; ``secret_args`` leading arguments are hidden from error messages."""
    try:
        proc = subprocess.run(
            ["git", *args],
            env=git_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoError(f"git {_shown(args, secret_args)} failed: {type(exc).__name__}") from None
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[-500:]
        raise RepoError(f"git {_shown(args, secret_args)} failed: {stderr}")
    return proc.stdout


def _shown(args: list[str], secret_args: int) -> str:
    visible = args[secret_args:]
    return " ".join(visible[:4]) + (" ..." if len(visible) > 4 else "")


def mirror_path(settings: Settings, repo_id: int) -> Path:
    """``<data_dir>/mirrors/<repo_id>.git``."""
    return settings.mirrors_dir / f"{repo_id}.git"


def local_external_id(path: Path) -> str:
    """sha256 hex of the resolved source path (the ``external_id`` of a local repo)."""
    return hashlib.sha256(str(Path(path).expanduser().resolve()).encode()).hexdigest()


def _is_git_repo(path: Path) -> bool:
    try:
        _git(["-C", str(path), "rev-parse", "--git-dir"], timeout=30)
    except RepoError:
        return False
    return True


def _source_head_branch(source: Path) -> str:
    try:
        ref = _git(["-C", str(source), "symbolic-ref", "--quiet", "HEAD"], timeout=30).decode().strip()
    except RepoError:
        return "main"
    return ref.removeprefix("refs/heads/") or "main"


async def register_local_repo(session: AsyncSession, source: Path | str, *, name: str | None = None) -> Repo:
    """Find or create the ``forge='local'`` :class:`Repo` for a git repository path (bare or working)
    or a ``file://``/``https://`` git URL without credentials."""
    url = _source_url(source)
    if "://" not in url and not _is_git_repo(Path(url)):
        raise RepoError(f"{url} is not a git repository")
    external_id = hashlib.sha256(url.encode()).hexdigest()
    found = await session.scalar(select(Repo).where(Repo.forge == "local", Repo.external_id == external_id))
    if found is not None:
        if found.settings_json.get("source") != url:
            found.settings_json["source"] = url
        return found
    default = _source_head_branch(Path(url)) if "://" not in url else "main"
    leaf = url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git") or "repo"
    repo = Repo(
        forge="local",
        external_id=external_id,
        full_name=f"local/{name or leaf}",
        default_branch=default,
        private=True,
        language="python",
        settings_json={"source": url},
    )
    session.add(repo)
    await session.flush()
    return repo


def _source_url(source: Path | str) -> str:
    """A normalised source: an absolute resolved path, or a credential-free https/file URL."""
    text = str(source)
    if "://" in text:
        scheme = text.split("://", 1)[0].lower()
        if scheme not in ("https", "file") or "@" in text.split("://", 1)[1].split("/", 1)[0]:
            raise RepoError("repository URLs must be https:// or file:// without credentials")
        return text
    resolved = Path(text).expanduser().resolve()
    if not resolved.is_dir():
        raise RepoError(f"{resolved} is not a directory")
    return str(resolved)


def ensure_mirror(
    settings: Settings,
    repo_source: str | Path | None,
    repo_row: Repo,
    *,
    credentials: MirrorCredentials | None = None,
) -> Path:
    """Create or refresh the bare mirror of ``repo_row`` and return its path.

    The source is ``repo_source``, else the ``source`` recorded in the repo's settings (local repos,
    and forge repos pointed at a local copy such as the demo); otherwise a forge repo is fetched from
    the forge's canonical HTTPS URL with ``credentials`` as an extra header.
    """
    mirror = mirror_path(settings, repo_row.id)
    secret: list[str] = credentials.git_config_args() if credentials is not None else []
    recorded = repo_source or (repo_row.settings_json or {}).get("source")
    if recorded:
        url = _source_url(recorded)
        secret = []
    elif repo_row.forge == "local":
        raise RepoError(f"local source of {repo_row.full_name} is not recorded")
    else:
        host = FORGE_GIT_HOSTS.get(repo_row.forge)
        if host is None:
            raise RepoError(f"no git host known for forge {repo_row.forge!r}")
        url = f"{host}/{repo_row.full_name}.git"
    if not (mirror / "HEAD").is_file():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(dir=mirror.parent, prefix=f".{mirror.name}.tmp-"))
        try:
            target = staging / "repo.git"
            _git([*secret, "clone", "--mirror", "--quiet", "--", url, str(target)], secret_args=len(secret))
            if mirror.exists():
                shutil.rmtree(mirror)
            target.rename(mirror)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
    else:
        _git(
            [*secret, "--git-dir", str(mirror), "fetch", "--prune", "--quiet", "--", url,
             "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"],
            secret_args=len(secret),
        )  # fmt: skip
    return mirror


async def ensure_mirror_async(
    settings: Settings, repo_row: Repo, *, source: str | Path | None = None, forge: Forge | None = None
) -> Path:
    """:func:`ensure_mirror` off the event loop; forge repos get fresh mirror credentials first."""
    credentials = None
    recorded = source or (repo_row.settings_json or {}).get("source")
    if repo_row.forge != "local" and not recorded and forge is not None:
        from coverset.forge.base import repo_ref_from_model

        credentials = await forge.fetch_mirror_credentials(
            repo_row.installation_id, repo_ref_from_model(repo_row)
        )
    return await asyncio.to_thread(ensure_mirror, settings, source, repo_row, credentials=credentials)


def resolve_commit(mirror: Path, ref: str) -> str:
    """The 40-hex commit SHA a branch name, tag or SHA resolves to in ``mirror``."""
    try:
        validate_ref(ref, repo=mirror)
    except GitDiffError as exc:
        raise RepoError(str(exc)) from None
    out = _git(
        [
            "--git-dir",
            str(mirror),
            "rev-parse",
            "--verify",
            "--quiet",
            "--end-of-options",
            f"{ref}^{{commit}}",
        ]
    )
    sha = out.decode().strip()
    if not _SHA_RE.fullmatch(sha):
        raise RepoError(f"{ref!r} does not name a commit")
    return sha


def default_branch_sha(mirror: Path, branch: str) -> str:
    """The commit at the tip of ``branch``."""
    return resolve_commit(mirror, f"refs/heads/{branch}")


def merge_base(mirror: Path, a: str, b: str) -> str | None:
    """``git merge-base a b`` or None when the commits share no history."""
    try:
        out = _git(["--git-dir", str(mirror), "merge-base", "--end-of-options", a, b])
    except RepoError:
        return None
    sha = out.decode().strip()
    return sha if _SHA_RE.fullmatch(sha) else None


def archive(mirror: Path, sha: str) -> bytes:
    """``git archive --format=tar.gz <sha>``: the commit's tree, without ``.git``."""
    if not _SHA_RE.fullmatch(sha):
        raise RepoError(f"archive needs a full commit SHA, got {sha!r}")
    return _git(["--git-dir", str(mirror), "archive", "--format=tar.gz", sha])


def tree_paths(mirror: Path, sha: str) -> set[str]:
    """Every file path in the tree of ``sha``."""
    out = _git(["--git-dir", str(mirror), "ls-tree", "-r", "-z", "--name-only", sha])
    return {name for name in out.decode("utf-8", "surrogateescape").split("\0") if name}


def read_blob(mirror: Path, sha: str, path: str) -> bytes | None:
    """Contents of ``path`` at ``sha``, or None when the commit has no such file."""
    return read_blobs(mirror, sha, [path]).get(path)


def read_blobs(mirror: Path, sha: str, paths: Iterable[str]) -> dict[str, bytes]:
    """Contents of each existing ``path`` at ``sha`` (one ``git cat-file --batch`` process)."""
    wanted = [p for p in dict.fromkeys(paths) if p and "\n" not in p]
    if not wanted:
        return {}
    request = "".join(f"{sha}:{p}\n" for p in wanted).encode("utf-8", "surrogateescape")
    try:
        proc = subprocess.run(
            ["git", "--git-dir", str(mirror), "cat-file", "--batch"],
            input=request,
            env=git_env(),
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RepoError(f"git cat-file failed: {type(exc).__name__}") from None
    if proc.returncode != 0:
        raise RepoError(f"git cat-file failed: {proc.stderr.decode('utf-8', 'replace')[-300:]}")
    return _parse_batch(proc.stdout, wanted)


def _parse_batch(data: bytes, wanted: list[str]) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    offset = 0
    for path in wanted:
        newline = data.index(b"\n", offset)
        header = data[offset:newline].split()
        offset = newline + 1
        if len(header) == 2 and header[1] == b"missing":
            continue
        if len(header) != 3:
            raise RepoError(f"unexpected git cat-file output for {path!r}")
        size = int(header[2])
        if header[1] == b"blob":
            result[path] = data[offset : offset + size]
        offset += size + 1
    return result


def extract_archive(data: bytes, dest: Path) -> Path:
    """Extract a ``git archive`` tarball into ``dest`` (``data`` filter: no links out, no devices)."""
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            tar.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as exc:
        raise RepoError(f"cannot extract the archive: {exc}") from exc
    return dest
