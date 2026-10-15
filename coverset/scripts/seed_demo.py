"""Build the deterministic demo git repository from examples/demo-app and examples/demo-prs.

The bare repository gets six commits on ``main`` that build the ledger app up package by
package (the sixth is the full examples/demo-app tree) and one branch per demo pull
request, each a single commit applying that PR's overlay on top of ``main``. Author,
committer, dates, file modes and line endings are fixed, so the commit SHAs are the same
on every machine for the same input files. The SHAs are written to ``<out>.shas.json``.

Only files git would track are used: the tree is listed with ``git ls-files``, so anything the
.gitignore files exclude (virtualenvs, caches, coverage output, ``.env``) never reaches the demo
repository, and secret-looking paths are refused outright. The repository is built next to
``<out>`` and swapped in only once it is complete; an existing ``<out>`` is replaced only if it
is a bare repository with its ``.shas.json`` manifest, i.e. one this script made.

Usage: ``uv run python scripts/seed_demo.py [--out data/demo.git] [--work <dir>]``; ``--work``
keeps a copy of the fast-import stream in that directory for inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEMO_APP = PROJECT_ROOT / "examples" / "demo-app"
DEMO_PRS = PROJECT_ROOT / "examples" / "demo-prs"
DEFAULT_OUT = PROJECT_ROOT / "data" / "demo.git"

AUTHOR = "Demo Author <demo@coverset.invalid>"
EPOCH = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
MAIN = "main"

SECRET_PATTERNS = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", "secrets")
SAFE_PATH = re.compile(r"[A-Za-z0-9._/-]+")
PR_DIR = re.compile(r"(\d+)-([a-z0-9-]+)")

# Each main commit adds the demo-app paths matching its prefixes; the last one adds the rest.
MAIN_COMMITS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Add ledger project skeleton and util helpers",
        (
            ".gitignore",
            "README.md",
            "pyproject.toml",
            "uv.lock",
            "src/ledger/__init__.py",
            "src/ledger/util/",
            "tests/conftest.py",
            "tests/test_dates.py",
            "tests/test_money.py",
            "tests/test_text.py",
        ),
    ),
    (
        "Add cent allocation, tax and invoices",
        ("src/ledger/billing/", "tests/test_allocate.py", "tests/test_invoices.py", "tests/test_tax.py"),
    ),
    (
        "Add duration parsing, formatting and expiry windows",
        ("src/ledger/durations/", "tests/test_format.py", "tests/test_parse.py", "tests/test_windows.py"),
    ),
    (
        "Add email validation, the signup rule and roles",
        ("src/ledger/accounts/", "tests/test_emailcheck.py", "tests/test_roles.py", "tests/test_signup.py"),
    ),
    (
        "Add interval merging and overlap helpers",
        ("src/ledger/intervals/", "tests/test_merge.py", "tests/test_overlap.py"),
    ),
    ("Declare invariants and add property tests", ("",)),
)


@dataclass(frozen=True)
class SeedResult:
    """The SHAs of a seeded demo repository."""

    commits: dict[str, str]
    branches: dict[str, str]
    prs: list[dict[str, object]]

    def to_json(self) -> str:
        """Serialise as the ``<out>.shas.json`` manifest."""
        payload = {"commits": self.commits, "branches": self.branches, "prs": self.prs}
        return json.dumps(payload, indent=2) + "\n"


@dataclass(frozen=True)
class DemoPR:
    """One demo pull request: an overlay applied to ``main`` as a single commit."""

    number: int
    branch: str
    title: str
    body: str
    files: dict[str, bytes]
    deletions: tuple[str, ...]


def read_tree(root: Path) -> dict[str, bytes]:
    """Return every file git would track under ``root`` as ``{posix relative path: LF bytes}``.

    ``root`` must be inside a git work tree. Tracked files plus untracked files that no
    .gitignore excludes are listed; files deleted from the work tree are skipped. Symlinks,
    paths git would need to quote and secret-looking paths (``.env``, ``*.pem``, ``secrets/``)
    are errors.
    """
    tree: dict[str, bytes] = {}
    for rel in list_files(root):
        path = root / rel
        if path.is_symlink():
            raise ValueError(f"symlinks are not allowed in the demo tree: {rel}")
        if not path.exists():
            continue
        if not SAFE_PATH.fullmatch(rel):
            raise ValueError(f"unsupported characters in demo path: {rel!r}")
        if any(fnmatch(part, pattern) for part in rel.split("/") for pattern in SECRET_PATTERNS):
            raise ValueError(f"refusing to put a secret-looking file in the demo repository: {rel}")
        tree[rel] = path.read_bytes().replace(b"\r\n", b"\n")
    return tree


def list_files(root: Path) -> list[str]:
    """Sorted paths, relative to ``root``, of the files ``git ls-files`` lists as tracked or addable."""
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "."],
        capture_output=True,
        env=git_env(),
        check=False,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"cannot list the files of {root}; it must be inside a git work tree: {detail}")
    return sorted({name for name in proc.stdout.decode().split("\0") if name})


def main_snapshots(app: Mapping[str, bytes]) -> list[tuple[str, dict[str, bytes]]]:
    """Split the demo-app tree into the cumulative trees of the six main commits."""
    snapshots: list[tuple[str, dict[str, bytes]]] = []
    included: set[str] = set()
    for message, prefixes in MAIN_COMMITS:
        added = {path for path in app if path.startswith(prefixes)} - included
        if not added:
            raise ValueError(f"commit {message!r} adds no files")
        included |= added
        snapshots.append((message, {path: app[path] for path in sorted(included)}))
    return snapshots


def load_prs(prs_dir: Path) -> list[DemoPR]:
    """Read every ``NN-slug`` overlay directory under ``prs_dir`` in number order."""
    prs: list[DemoPR] = []
    for entry in sorted(prs_dir.iterdir()):
        match = PR_DIR.fullmatch(entry.name)
        if not entry.is_dir() or match is None:
            continue
        number = int(match.group(1))
        title_line, _, body = (entry / "PR.md").read_text(encoding="utf-8").partition("\n")
        if not title_line.startswith("# "):
            raise ValueError(f"{entry / 'PR.md'} must start with '# <title>'")
        delete_file = entry / "DELETE"
        deletions = ()
        if delete_file.exists():
            lines = delete_file.read_text(encoding="utf-8").splitlines()
            deletions = tuple(line.strip() for line in lines if line.strip())
        prs.append(
            DemoPR(
                number=number,
                branch=f"pr-{number}-{match.group(2)}",
                title=title_line.removeprefix("# ").strip(),
                body=body.strip(),
                files=read_tree(entry / "files"),
                deletions=deletions,
            )
        )
    return prs


def apply_overlay(base: Mapping[str, bytes], pr: DemoPR) -> dict[str, bytes]:
    """Return ``base`` with the PR's files copied over it and its deletions removed."""
    missing = [path for path in pr.deletions if path not in base]
    if missing:
        raise ValueError(f"{pr.branch} deletes paths that do not exist: {missing}")
    tree = {path: data for path, data in base.items() if path not in pr.deletions}
    tree.update(pr.files)
    return dict(sorted(tree.items()))


class FastImportStream:
    """Builds a ``git fast-import`` stream of full-tree commits with fixed identity and dates."""

    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._marks = 0

    def commit(self, ref: str, message: str, tree: Mapping[str, bytes], parent: int | None, hour: int) -> int:
        """Append a commit of exactly ``tree`` to ``ref`` and return its mark."""
        self._marks += 1
        stamp = int((EPOCH + timedelta(hours=hour)).timestamp())
        out = [
            f"commit refs/heads/{ref}\n".encode(),
            f"mark :{self._marks}\n".encode(),
            f"author {AUTHOR} {stamp} +0000\n".encode(),
            f"committer {AUTHOR} {stamp} +0000\n".encode(),
            _data(f"{message}\n".encode()),
        ]
        if parent is not None:
            out.append(f"from :{parent}\n".encode())
        out.append(b"deleteall\n")
        for path in sorted(tree):
            out.append(f"M 100644 inline {path}\n".encode())
            out.append(_data(tree[path]))
        out.append(b"\n")
        self._chunks.extend(out)
        return self._marks

    def getvalue(self) -> bytes:
        """Return the stream, terminated with ``done``."""
        return b"".join([*self._chunks, b"done\n"])


def _data(payload: bytes) -> bytes:
    return f"data {len(payload)}\n".encode() + payload + b"\n"


def git_env() -> dict[str, str]:
    """Environment for git that ignores user/system config and any inherited repository."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, LC_ALL="C")
    return env


def git(git_dir: Path, *args: str, stdin: bytes | None = None) -> str:
    """Run git against the bare repository ``git_dir`` and return its stripped stdout."""
    proc = subprocess.run(
        ["git", "-c", "core.autocrlf=false", "--git-dir", str(git_dir), *args],
        input=stdin,
        capture_output=True,
        env=git_env(),
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.decode(errors='replace').strip()}")
    return proc.stdout.decode().strip()


def shas_path(out: Path) -> Path:
    """Path of the SHA manifest written next to the bare repository ``out``."""
    return out.with_name(out.name + ".shas.json")


def seed(
    out: Path, work: Path | None = None, *, app: Path = DEMO_APP, prs_dir: Path = DEMO_PRS
) -> SeedResult:
    """(Re)create the bare demo repository at ``out`` from ``app`` and ``prs_dir``; return its SHAs.

    The repository is built in a sibling temporary directory and moved into place only when
    complete, and the SHAs are written to ``<out>.shas.json`` the same way, so a reader never
    sees a half-built repository. An existing ``out`` must be empty or a repository this
    function made (bare, with its manifest); anything else raises ValueError and is left
    untouched. When ``work`` is given, the fast-import stream is kept there as
    ``demo.fast-import``.
    """
    _check_replaceable(out)
    snapshots = main_snapshots(read_tree(app))
    prs = load_prs(prs_dir)

    stream = FastImportStream()
    mark: int | None = None
    for hour, (message, tree) in enumerate(snapshots):
        mark = stream.commit(MAIN, message, tree, mark, hour)
    main_tree = snapshots[-1][1]
    for offset, pr in enumerate(prs, start=len(snapshots)):
        stream.commit(pr.branch, f"{pr.title}\n\n{pr.body}", apply_overlay(main_tree, pr), mark, offset)
    payload = stream.getvalue()
    if work is not None:
        work.mkdir(parents=True, exist_ok=True)
        (work / "demo.fast-import").write_bytes(payload)

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=out.parent, prefix=f".{out.name}.tmp-"))
    try:
        result = _build(staging / "repo.git", payload, prs)
        manifest = staging / "shas.json"
        manifest.write_text(result.to_json(), encoding="utf-8")
        _swap_in(staging / "repo.git", out)
        os.replace(manifest, shas_path(out))
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return result


def _check_replaceable(out: Path) -> None:
    """Raise ValueError unless ``out`` is absent, an empty directory or a seeded bare repository."""
    if out.is_symlink() or (out.exists() and not out.is_dir()):
        raise ValueError(f"{out} exists and is not a directory; refusing to replace it")
    if not out.exists() or not any(out.iterdir()):
        return
    try:
        bare = git(out, "rev-parse", "--is-bare-repository") == "true"
    except RuntimeError:
        bare = False
    if not (bare and shas_path(out).is_file()):
        raise ValueError(
            f"{out} is not a demo repository made by this script (a bare repository with "
            f"{shas_path(out).name} beside it); refusing to replace it"
        )


def _build(git_dir: Path, payload: bytes, prs: list[DemoPR]) -> SeedResult:
    """Create the bare repository ``git_dir`` from the fast-import ``payload`` and read its SHAs."""
    init = subprocess.run(
        ["git", "init", "--quiet", "--bare", "--template=", "--initial-branch", MAIN, str(git_dir)],
        capture_output=True,
        env=git_env(),
        check=False,
    )
    if init.returncode != 0:
        raise RuntimeError(f"git init failed: {init.stderr.decode(errors='replace').strip()}")
    git(git_dir, "fast-import", "--quiet", "--done", stdin=payload)

    main_shas = git(git_dir, "rev-list", "--reverse", MAIN).split()
    heads = {pr.branch: git(git_dir, "rev-parse", f"refs/heads/{pr.branch}") for pr in prs}
    return SeedResult(
        commits={f"main-{n}": sha for n, sha in enumerate(main_shas, start=1)},
        branches={MAIN: main_shas[-1], **heads},
        prs=[
            {
                "number": pr.number,
                "branch": pr.branch,
                "title": pr.title,
                "body": pr.body,
                "base_sha": main_shas[-1],
                "head_sha": heads[pr.branch],
            }
            for pr in prs
        ],
    )


def _swap_in(built: Path, out: Path) -> None:
    """Move the complete repository ``built`` to ``out``.

    A previous ``out`` is first moved next to ``built`` (inside the staging directory the caller
    removes) and restored if the move fails.
    """
    if not out.exists():
        built.rename(out)
        return
    previous = built.with_name("previous.git")
    out.rename(previous)
    try:
        built.rename(out)
    except OSError:
        previous.rename(out)
        raise


def main(argv: list[str] | None = None) -> int:
    """Command-line entry point."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="bare repository to (re)create")
    parser.add_argument("--work", type=Path, default=None, help="directory to keep the fast-import stream in")
    args = parser.parse_args(argv)
    try:
        result = seed(args.out.resolve(), args.work)
    except ValueError as exc:
        parser.error(str(exc))
    for name, sha in {**result.commits, **result.branches}.items():
        print(f"{sha}  {name}")
    print(f"wrote {shas_path(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
