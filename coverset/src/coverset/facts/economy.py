"""The test-economy facts: computed at ``finalize_run``, read back as dataclasses.

:func:`write_economy` is the one hook :func:`coverset.facts.ingest.finalize_run` calls. It loads
the run as :class:`~coverset.analysis.economy.EconomyData` straight from the Parquet columns
(:func:`economy_data`: numpy arrays, no Python object per covered line), reads the test files,
conftests, helper modules and production files at the head commit from the repository's git mirror
(``<data_dir>/mirrors/<repo_id>.git``, the directory beside the facts store) in a few batches, finds
a base run for the PR test tax (:func:`base_from_facts`), wires git history for stale tests
(:func:`git_stale_lookup`), runs :func:`coverset.analysis.economy.analyse_data` and writes the four
run tables
``test_economy``, ``test_families``, ``func_testing`` and ``run_economy``
(:data:`~coverset.facts.schema.ECONOMY_TABLES`). It never raises: a failure is logged and recorded
as a note in ``run_economy`` (when even that cannot be written, the tables are simply absent and the
readers return None or empty lists, which the pages show as "not computed").

Without a mirror (a store used on its own, tests, ``coverset report`` over a facts run) everything
that needs no source is still computed (coverage value, uniqueness, redundancy, families, direct
and indirect testing); test weights are "not measured", handlers, stale tests and the statement
diff are skipped, and ``run_economy.notes_json`` says so.

The readers (:func:`run_economy`, :func:`tests_economy`, :func:`redundancy`, :func:`families`,
:func:`func_testing`, :func:`pr_test_tax`) are what the Economy page, the PR card, the sticky
comment, the CLI and the agent API call. ``docs/economy.md`` documents every one of them.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Protocol

import numpy as np
import pyarrow as pa

from coverset.analysis.economy import (
    ARC_WEIGHT,
    CLASS_WEIGHT,
    LINE_WEIGHT,
    REMOVABLE_CAVEAT,
    BaseSnapshot,
    EconomyData,
    EconomyPolicy,
    EconomyReport,
    FamilyEconomy,
    FunctionTesting,
    Handler,
    RunTotals,
    StaleLookup,
    TestTax,
    analyse_data,
    conftest_paths,
    helper_module_candidates,
    is_test_path,
    observed_classes,
    tax_message,
)
from coverset.analysis.functions import index_functions
from coverset.facts.schema import ECONOMY_TABLES, SCHEMAS
from coverset.facts.store import FactsStore
from coverset.lang.base import (
    IMPORT_CONTEXT,
    FunctionRegion,
    FuncTriple,
    ShapeObs,
    StaticFile,
)
from coverset.logging import redact

if TYPE_CHECKING:
    from coverset.facts.ingest import RunContext

__all__ = [
    "ECONOMY_TABLES",
    "REMOVABLE_CAVEAT",
    "EconomyInputs",
    "EquivalenceGroup",
    "GitHistory",
    "Redundancy",
    "RepoHistory",
    "RunEconomy",
    "TestEconomyRow",
    "base_from_facts",
    "compute_economy",
    "economy_data",
    "families",
    "func_testing",
    "git_stale_lookup",
    "mirror_history",
    "policy_from_scopes",
    "pr_test_tax",
    "run_economy",
    "tests_economy",
    "write_economy",
    "write_report",
]

log = logging.getLogger(__name__)

MAX_SOURCE_FILES: Final = 5_000
"""Files read from the mirror per commit for one run; beyond it the rest are "not measured"."""
MAX_SOURCE_BYTES: Final = 2_000_000
"""Bigger files (generated code, vendored bundles) are skipped."""
MAX_HISTORY_PROCESSES: Final = 64
"""git processes one run's economy may start (reads and history walks); beyond it the rest of the
stale-test check is "not measured"."""
MAX_BASE_CANDIDATES: Final = 500
"""The most recent runs searched for the base run of the PR test tax."""
MAX_TEST_LINE_ROWS: Final = 25_000_000
"""Runs with more ``test_lines`` rows skip the economy (noted): about 50 bytes and 0.3 s of
analysis per row-million, so 25M rows is roughly 1.3 GB and 8 s under the run lock."""

_SHA = re.compile(r"^[0-9a-f]{40}$")


# -- repository access -------------------------------------------------------------------------


class RepoHistory(Protocol):
    """What the economy reads from git: file text at a commit and the paths' last changes."""

    def read(self, sha: str, paths: Iterable[str]) -> dict[str, str]:
        """Text of each existing path at ``sha`` (missing and oversized files left out)."""
        ...

    def read_at(self, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """Text of each existing ``(commit, path)``, any mix of commits, in one go."""
        ...

    def last_changes(self, sha: str, paths: Iterable[str]) -> dict[str, tuple[str, int]]:
        """``(commit, unix time)`` of the last commit at or before ``sha`` that changed each path
        (paths with no such commit, or beyond the history budget, are left out)."""
        ...


class GitHistory:
    """:class:`RepoHistory` over one git directory: a bare mirror, or a work tree's ``.git``.

    Git runs with the control plane's scrubbed environment (``orchestrator.repos.git_env``): no user
    config, no credentials, no prompts. Only full 40-hex SHAs are accepted, and paths always follow
    ``--`` so a path can never be read as an option. :meth:`last_changes` answers many paths with one
    ``git log --name-only`` walk per chunk of paths, stopped as soon as every path has been seen, so
    a run's stale-test check costs a few processes, not one per file.
    """

    LOG_CHUNK: Final = 400
    """Paths per ``git log`` process (keeps the argument list far below ``ARG_MAX``)."""

    def __init__(self, git_dir: Path, *, timeout_s: float = 60.0) -> None:
        self.git_dir = Path(git_dir)
        self.timeout_s = timeout_s
        self._changes: dict[tuple[str, str], tuple[str, int] | None] = {}
        self.processes = 0

    def read(self, sha: str, paths: Iterable[str]) -> dict[str, str]:
        found = self.read_at((sha, p) for p in paths)
        return {path: text for (_, path), text in found.items()}

    def read_at(self, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], str]:
        """One ``git cat-file --batch`` for every ``(commit, path)``; non-SHA commits and paths
        with a newline are skipped, as are files over :data:`MAX_SOURCE_BYTES`."""
        from coverset.orchestrator.repos import git_env

        wanted = [(c, p) for c, p in dict.fromkeys(pairs) if _SHA.match(c) and p and "\n" not in p]
        wanted = wanted[:MAX_SOURCE_FILES]
        if not wanted:
            return {}
        self.processes += 1
        request = "".join(f"{c}:{p}\n" for c, p in wanted).encode("utf-8", "surrogateescape")
        try:
            proc = subprocess.run(
                ["git", "--git-dir", str(self.git_dir), "cat-file", "--batch"],
                input=request,
                env=git_env(),
                capture_output=True,
                timeout=self.timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("economy: git cat-file failed: %s", type(exc).__name__)
            return {}
        if proc.returncode != 0:
            log.warning("economy: git cat-file failed: %s", proc.stderr.decode("utf-8", "replace")[-300:])
            return {}
        return _parse_cat_file(proc.stdout, wanted)

    def last_changes(self, sha: str, paths: Iterable[str]) -> dict[str, tuple[str, int]]:
        if not _SHA.match(sha):
            return {}
        wanted = list(dict.fromkeys(paths))
        todo = [p for p in wanted if (sha, p) not in self._changes and "\n" not in p]
        for start in range(0, len(todo), self.LOG_CHUNK):
            if self.processes >= MAX_HISTORY_PROCESSES:
                break
            chunk = todo[start : start + self.LOG_CHUNK]
            found = self._walk(sha, chunk)
            for path in chunk:
                self._changes[(sha, path)] = found.get(path)
        return {p: v for p in wanted if (v := self._changes.get((sha, p))) is not None}

    def _walk(self, sha: str, paths: Sequence[str]) -> dict[str, tuple[str, int]]:
        """One ``git log --name-only`` from ``sha`` limited to ``paths``, newest first, stopped once
        every path has been attributed to its most recent commit."""
        from coverset.orchestrator.repos import git_env

        self.processes += 1
        args = ["git", "-c", "core.quotePath=false", "--git-dir", str(self.git_dir), "log"]
        args += ["--format=%x00%H %ct", "--name-only", "--no-renames", sha, "--", *paths]
        found: dict[str, tuple[str, int]] = {}
        wanted = set(paths)
        deadline = time.monotonic() + self.timeout_s
        try:
            proc = subprocess.Popen(
                args,
                env=git_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            return found
        assert proc.stdout is not None
        buffer = b""
        try:
            while len(found) < len(wanted) and time.monotonic() < deadline:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                buffer += chunk
                records = buffer.split(b"\0")
                buffer = records.pop()  # the last record may be incomplete
                for record in records:
                    _attribute(record, wanted, found)
            else:
                buffer = b""
            if buffer:
                _attribute(buffer, wanted, found)
        finally:
            proc.kill()
            proc.wait()
            proc.stdout.close()
        return found


def _parse_cat_file(data: bytes, wanted: Sequence[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """``git cat-file --batch`` output, one answer per request in order: ``<oid> blob <size>`` and
    the content, or ``<name> missing`` (also ``ambiguous``, and non-blobs, which are skipped)."""
    found: dict[tuple[str, str], str] = {}
    offset = 0
    for key in wanted:
        newline = data.find(b"\n", offset)
        if newline < 0:
            break
        header = data[offset:newline].split()
        offset = newline + 1
        if len(header) != 3 or not header[2].isdigit():
            continue  # missing or ambiguous: no content follows
        size = int(header[2])
        if header[1] == b"blob" and size <= MAX_SOURCE_BYTES:
            found[key] = data[offset : offset + size].decode("utf-8", "replace")
        offset += size + 1
    return found


def _attribute(record: bytes, wanted: set[str], found: dict[str, tuple[str, int]]) -> None:
    """Credit one ``git log`` record (``<sha> <time>\\n\\n<path>\\n...``) to the paths it names
    that no newer record has claimed."""
    lines = record.decode("utf-8", "surrogateescape").split("\n")
    header = lines[0].split()
    if len(header) != 2 or not _SHA.match(header[0]) or not header[1].isdigit():
        return
    for path in lines[1:]:
        if path in wanted and path not in found:
            found[path] = (header[0], int(header[1]))


def mirror_history(store: FactsStore, repo_id: int) -> GitHistory | None:
    """The repository's bare mirror as :class:`GitHistory`, when it sits beside the facts store.

    ``Settings`` puts ``facts`` and ``mirrors`` side by side under the data directory
    (``config.Settings.facts_dir``, ``mirrors_dir``), so the store's parent locates the mirror
    without any settings object reaching the facts layer.
    """
    mirror = store.root.parent / "mirrors" / f"{repo_id}.git"
    return GitHistory(mirror) if (mirror / "HEAD").is_file() else None


def policy_from_scopes(policy: object | None) -> EconomyPolicy:
    """:class:`EconomyPolicy` from a ``coverset.settings_scopes.Policy`` (``effective_policy``).

    The scoped policy is async and lives in the control database, which the facts layer never
    touches: the caller resolves it (the runner, before ``finalize_run``; a page, before
    :func:`pr_test_tax`) and hands the object over. Only ``test_tax_threshold`` is scoped today;
    anything without it gives the built-in defaults.
    """
    threshold = getattr(policy, "test_tax_threshold", None)
    if isinstance(threshold, int | float) and not isinstance(threshold, bool) and threshold > 0:
        return EconomyPolicy(test_tax_threshold=float(threshold))
    return EconomyPolicy()


@dataclass(frozen=True, slots=True)
class EconomyInputs:
    """What :func:`write_economy` takes beyond the facts (all optional).

    ``history`` defaults to :func:`mirror_history`; ``policy`` to the built-in thresholds (the
    readers re-flag the tax against the effective ``policy.test_tax_threshold`` when given one);
    ``durations`` (nodeid -> p50 seconds) replace the run's own durations.
    """

    history: RepoHistory | None = None
    policy: EconomyPolicy | None = None
    durations: Mapping[str, float] | None = None
    enabled: bool = True


# -- the facts as a bundle ---------------------------------------------------------------------


def _functions(store: FactsStore, repo_id: int, run_id: str) -> list[FunctionRegion]:
    return [
        FunctionRegion(
            func_key=row["func_key"],
            path=row["path"],
            qualname=row["qualname"],
            ordinal=row["ordinal"],
            def_line=row["def_line"],
            body_start=row["body_start"],
            end_line=row["end_line"],
            body_hash=row["body_hash"],
            sig_hash=row["sig_hash"],
            params=tuple(json.loads(row["params_json"] or "[]")),
            kind=row["kind"],
        )
        for row in store.read_table(repo_id, run_id, "functions").to_pylist()
    ]


def _tests_dim(
    store: FactsStore, repo_id: int, run_id: str
) -> dict[str, tuple[str, str | None, float | None]]:
    """``nodeid -> (file, outcome, duration_s)``; a test several shards recorded keeps its last shard's
    result (``<import>`` excluded)."""
    ids = store.dictionary(repo_id)
    found: dict[str, tuple[str, str | None, float | None]] = {}
    rows = store.read_table(repo_id, run_id, "tests_dim").to_pylist()
    for row in sorted(rows, key=lambda r: r["shard_id"]):
        nodeid = row["nodeid"]
        if nodeid == IMPORT_CONTEXT:
            continue
        file = ids.path(row["file_id"]) if row["file_id"] is not None else nodeid.split("::", 1)[0]
        previous = found.get(nodeid)
        outcome = row["outcome"] if row["outcome"] is not None else (previous[1] if previous else None)
        duration = row["duration_s"] if row["duration_s"] is not None else (previous[2] if previous else None)
        found[nodeid] = (file, outcome, duration)
    return found


def _static(store: FactsStore, repo_id: int, run_id: str) -> dict[str, StaticFile]:
    ids = store.dictionary(repo_id)
    possible: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for row in store.read_table(repo_id, run_id, "file_arcs_possible").to_pylist():
        possible[row["file_id"]].add((row["from_line"], row["to_line"]))
    static: dict[str, StaticFile] = {}
    for row in store.read_table(repo_id, run_id, "file_static").to_pylist():
        path = ids.path(row["file_id"])
        static.setdefault(
            path,
            StaticFile(
                path,
                row["n_lines"] or 0,
                tuple(row["statements"] or ()),
                tuple(row["excluded"] or ()),
                tuple(sorted(possible.get(row["file_id"], ()))),
            ),
        )
    return static


def _shapes(
    store: FactsStore, repo_id: int, run_id: str, functions: Sequence[FunctionRegion]
) -> list[ShapeObs]:
    """``call_shapes`` as observations, each function's triple from ``functions`` (else
    ``func_sampling``), as ``finalize_run`` rebuilds them for the input-class catalogue."""
    ids = store.dictionary(repo_id)
    triples: dict[str, FuncTriple] = {}
    for row in store.read_table(repo_id, run_id, "func_sampling").to_pylist():
        triples[row["func_key"]] = (row["path"], row["qualname"], row["firstlineno"])
    for region in functions:
        triples[region.func_key] = region.triple
    shapes: list[ShapeObs] = []
    for row in store.read_table(repo_id, run_id, "call_shapes").to_pylist():
        triple = triples.get(row["func_key"])
        if triple is None:
            continue
        caller: FuncTriple | str = (
            row["caller_key"]
            if row["caller_kind"] == "test"
            else (row["caller_path"], row["caller_qualname"], row["caller_line"])
        )
        shapes.append(
            ShapeObs(
                func=triple,
                param=row["param"],
                shape_key=ids.shape_key(row["shape_id"]),
                caller=caller,
                samples=row["samples"] or 0,
                tests=tuple(ids.nodeid(t) for t in row["test_ids"] or ()),
                truncated=bool(row["truncated"]),
                example=None,
                example_test=None,
            )
        )
    return shapes


def economy_data(
    store: FactsStore, repo_id: int, run_id: str, *, durations: Mapping[str, float] | None = None
) -> tuple[EconomyData, list[FunctionRegion]]:
    """One stored run as :class:`~coverset.analysis.economy.EconomyData`, plus its function regions.

    ``test_lines`` goes from Parquet to numpy without a Python object per row: dictionary test ids
    are mapped to test indices through one lookup array (``<import>`` becomes ``-1``), and file ids
    are used as file indices directly. ``durations`` (nodeid -> seconds) replace the run's own.
    """
    ids = store.dictionary(repo_id)
    functions = _functions(store, repo_id, run_id)
    dim = _tests_dim(store, repo_id, run_id)
    tests = tuple(sorted(dim))
    seconds = {n: d for n, (_, _, d) in dim.items() if d is not None}
    seconds.update(durations or {})

    lines_table = store.read_table(repo_id, run_id, "test_lines")
    test_col = lines_table.column("test_id").to_numpy().astype(np.int64)
    file_col = lines_table.column("file_id").to_numpy().astype(np.int32)
    line_col = lines_table.column("line").to_numpy().astype(np.int32)
    test_ids = [ids.find_test_id(n) for n in tests]
    size = max([int(test_col.max()) if test_col.size else 0, *(t for t in test_ids if t is not None)]) + 1
    lookup = np.full(size, -2, dtype=np.int32)
    lookup[0] = -1  # <import>
    for i, test_id in enumerate(test_ids):
        if test_id is not None:
            lookup[test_id] = i
    cov_test = lookup[test_col] if test_col.size else np.empty(0, dtype=np.int32)
    keep = cov_test >= -1
    static = _static(store, repo_id, run_id)
    file_ids = {int(f) for f in np.unique(file_col)} | {
        ids.file_id(p) for p in static if ids.find_file_id(p) is not None
    }
    run_arcs: dict[str, set[tuple[int, int]]] = defaultdict(set)
    for row in store.read_table(repo_id, run_id, "run_arcs").to_pylist():
        run_arcs[ids.path(row["file_id"])].add((row["from_line"], row["to_line"]))
    data = EconomyData(
        tests=tests,
        test_files=tuple(dim[n][0] for n in tests),
        outcomes=tuple(dim[n][1] for n in tests),
        durations_ms=tuple(seconds[n] * 1000.0 if n in seconds else None for n in tests),
        paths={f: ids.path(f) for f in sorted(file_ids)},
        cov_test=cov_test[keep],
        cov_file=file_col[keep],
        cov_line=line_col[keep],
        static=static,
        run_arcs={p: tuple(sorted(a)) for p, a in run_arcs.items()},
        shapes=tuple(_shapes(store, repo_id, run_id, functions)),
        functions=tuple(functions),
    )
    return data, functions


def base_from_facts(
    store: FactsStore,
    repo_id: int,
    run_id: str,
    *,
    sha: str | None,
    exact: bool,
    test_sources: Mapping[str, str],
    sources: Mapping[str, str],
) -> BaseSnapshot:
    """The :class:`~coverset.analysis.economy.BaseSnapshot` of a stored base run: the union of its
    tests' lines per file (numpy ``unique`` over ``test_lines``), its statements, its observed input
    classes (``call_shapes`` already carries each function's ``func_key``) and its nodeids."""
    ids = store.dictionary(repo_id)
    table = store.read_table(repo_id, run_id, "test_lines")
    test_col = table.column("test_id").to_numpy()
    keys = np.empty(0, dtype=np.int64)
    if table.num_rows:
        mask = test_col != 0
        keys = np.unique(
            (table.column("file_id").to_numpy()[mask].astype(np.int64) << 32)
            | table.column("line").to_numpy()[mask].astype(np.int64)
        )
    coverage: dict[str, set[int]] = defaultdict(set)
    for key in keys.tolist():
        coverage[ids.path(key >> 32)].add(key & 0xFFFFFFFF)
    functions = _functions(store, repo_id, run_id)
    dim = _tests_dim(store, repo_id, run_id)
    test_files = {file for file, _, _ in dim.values()}
    classes = observed_classes(_shapes(store, repo_id, run_id, functions), functions, test_files)
    return BaseSnapshot(
        run_id=run_id,
        sha=sha,
        exact=exact,
        coverage={p: frozenset(v) for p, v in coverage.items()},
        statements={p: frozenset(s.statements) for p, s in _static(store, repo_id, run_id).items()},
        classes=classes,
        tests=frozenset(dim),
        test_sources=dict(test_sources),
        sources=dict(sources),
    )


# -- computing and writing ---------------------------------------------------------------------


class _BatchedReader:
    """Sources at one commit, fetched in a few batches up front and then served from memory.

    The first batch is the test files, their conftests and the production files; then, twice (a
    helper module may import another), the helper modules those test files import
    (:func:`~coverset.analysis.economy.helper_module_candidates`). A path never fetched reads as
    None, so the analysis never starts a git process per file.
    """

    HELPER_ROUNDS: Final = 2

    def __init__(self, history: RepoHistory, sha: str, prefetch: Iterable[str]) -> None:
        wanted = list(dict.fromkeys(prefetch))
        self.cache: dict[str, str] = history.read(sha, wanted)
        fetched = set(wanted)
        fresh = [p for p in wanted if is_test_path(p)]
        for _ in range(self.HELPER_ROUNDS):
            candidates = {
                c
                for p in fresh
                if (text := self.cache.get(p)) is not None
                for c in helper_module_candidates(p, text)
            }
            fresh = sorted(candidates - fetched)
            if not fresh:
                break
            fetched.update(fresh)
            self.cache.update(history.read(sha, fresh))

    def __call__(self, path: str) -> str | None:
        return self.cache.get(path)


def _test_code_paths(test_files: Iterable[str]) -> list[str]:
    """The Python test files of a run and every conftest pytest may load for them."""
    files = sorted({p for p in test_files if p.endswith(".py")})
    conftests = {c for p in files for c in conftest_paths(p)}
    return [*files, *sorted(conftests - set(files))]


def _base_run(store: FactsStore, ctx: RunContext) -> tuple[str, str, bool] | None:
    """``(run id, head sha, exact)`` of the run the PR test tax compares against.

    Exact: the newest finalized run of the repo whose head is the PR's base commit. Otherwise the
    run's scoring basis (the prior run), labelled not exact. Never the run itself.
    """
    if ctx.base_sha is None:
        return None
    candidates: list[tuple[object, str, str]] = []
    recent = sorted(store.list_runs(ctx.repo_id), reverse=True)[:MAX_BASE_CANDIDATES]
    for run_id in recent:  # run ids are ULIDs: newest first
        if run_id == ctx.run_id:
            continue
        rows = store.read_table(ctx.repo_id, run_id, "runs_dim").to_pylist()
        if rows and rows[0]["finalized_at"] is not None and rows[0]["head_sha"]:
            candidates.append((rows[0]["created_at"], run_id, rows[0]["head_sha"]))
    exact = [c for c in candidates if c[2] == ctx.base_sha]
    if exact:
        _, run_id, sha = max(exact, key=lambda c: (c[0], c[1]))
        return run_id, sha, True
    for _, run_id, sha in candidates:
        if run_id == ctx.scoring_basis_run_id and sha != ctx.head_sha:
            return run_id, sha, False
    return None


def git_stale_lookup(history: RepoHistory, head_sha: str, notes: list[str]) -> StaleLookup:
    """The analysis's ``stale_lookup`` over git, in three batched steps.

    1. One history walk finds the last change of every test file and every production file involved
       (:meth:`RepoHistory.last_changes`).
    2. A production file last changed no later than a test file leaves every function in it
       unchanged for that test file; every other ``(test file's commit, production file)`` pair is
       read in one ``git cat-file --batch``.
    3. Each pair is indexed and its functions' body hashes are compared with head (a function absent
       then counts as changed). Commit times decide "no later", which a rebase can skew.
    """

    def lookup(
        requests: Mapping[str, Sequence[FunctionRegion]],
    ) -> dict[str, Mapping[str, str | None] | None]:
        prod_paths = sorted({r.path for regions in requests.values() for r in regions})
        changes = history.last_changes(head_sha, [*requests, *prod_paths])
        needed: set[tuple[str, str]] = set()
        for test_path, regions in requests.items():
            test_change = changes.get(test_path)
            if test_change is None:
                continue
            for path in {r.path for r in regions}:
                change = changes.get(path)
                if change is not None and change[1] > test_change[1]:
                    needed.add((test_change[0], path))
        old = history.read_at(sorted(needed)) if needed else {}
        hashes: dict[tuple[str, str], dict[str, str]] = {
            pair: {r.func_key: r.body_hash for r in index_functions(pair[1], text)}
            for pair, text in old.items()
        }
        answers: dict[str, Mapping[str, str | None] | None] = {}
        missing_history = 0
        for test_path, regions in requests.items():
            test_change = changes.get(test_path)
            if test_change is None:
                answers[test_path] = None
                missing_history += 1
                continue
            then: dict[str, str | None] = {}
            for region in regions:
                change = changes.get(region.path)
                if change is None:
                    continue  # no history for the production file: count it as unchanged
                if change[1] <= test_change[1]:
                    then[region.func_key] = region.body_hash
                else:
                    then[region.func_key] = hashes.get((test_change[0], region.path), {}).get(region.func_key)
            answers[test_path] = then
        if missing_history:
            notes.append(
                f"stale tests not measured for {missing_history} test file(s) without readable history"
            )
        return answers

    return lookup


def compute_economy(store: FactsStore, ctx: RunContext, inputs: EconomyInputs | None = None) -> EconomyReport:
    """Run the analysis for one stored run (see the module docstring); raises on unexpected errors."""
    inputs = inputs or EconomyInputs()
    history = inputs.history if inputs.history is not None else mirror_history(store, ctx.repo_id)
    data, functions = economy_data(store, ctx.repo_id, ctx.run_id, durations=inputs.durations)
    extra: list[str] = []
    reader: Callable[[str], str | None] | None = None
    base: BaseSnapshot | None = None
    stale: StaleLookup | None = None
    if history is None:
        extra.append("no git mirror beside the facts store: nothing was read from the repository")
    elif not _SHA.match(ctx.head_sha):
        extra.append("head is not a full commit SHA: sources not read")
        history = None
    if history is not None:
        test_code = _test_code_paths(data.test_files)
        prod = sorted(p for p in data.static if p.endswith(".py") and not is_test_path(p, data.test_files))
        reader = _BatchedReader(history, ctx.head_sha, [*test_code, *prod])
        stale = git_stale_lookup(history, ctx.head_sha, extra)
        found = _base_run(store, ctx)
        if found is not None:
            base_run_id, base_sha, exact = found
            base_files = [row[0] for row in _tests_dim(store, ctx.repo_id, base_run_id).values()]
            base_tests = _test_code_paths(base_files)
            covered = {data.paths[int(f)] for f in np.unique(data.cov_file)} if data.cov_file.size else set()
            base = base_from_facts(
                store,
                ctx.repo_id,
                base_run_id,
                sha=base_sha,
                exact=exact,
                test_sources=history.read(base_sha, dict.fromkeys([*test_code, *base_tests])),
                sources=history.read(base_sha, sorted(covered & set(prod))),
            )
            if not exact:
                extra.append(f"no run of the base commit: the PR test tax compares with run {base_run_id}")
    report = analyse_data(
        data, reader, functions=functions, base=base, stale_lookup=stale, policy=inputs.policy
    )
    if extra:
        report = dataclasses.replace(report, notes=(*extra, *report.notes))
    return report


def _test_economy_table(report: EconomyReport, test_id: Callable[[str], int]) -> pa.Table:
    rows = []
    for t in report.tests:
        w = t.weight
        rows.append(
            {
                "test_id": test_id(t.nodeid),
                "measured": w.measured,
                "reason": w.reason,
                "statements": w.statements,
                "asserts": w.asserts,
                "helper_asserts": w.helper_asserts,
                "raises": w.raises,
                "fixtures": w.fixtures,
                "helper_statements": w.helper_statements,
                "file_lines": w.file_lines,
                "cases": t.cases,
                "lines": t.lines,
                "arcs": t.arcs,
                "classes": t.classes,
                "observations": t.observations,
                "unique_lines": t.unique_lines,
                "unique_arcs": t.unique_arcs,
                "unique_classes": t.unique_classes,
                "unique_facts": t.unique_facts,
                "leverage": t.leverage,
                "duration_ms": t.duration_ms,
                "cost_per_fact_ms": t.cost_per_fact_ms,
                "signature": t.signature,
                "group_id": t.group_id,
                "dominated_by": test_id(t.dominated_by) if t.dominated_by is not None else None,
                "in_minimal_set": t.in_minimal_set,
                "removable": t.removable,
                "assertion_free": t.assertion_free,
                "stale": t.stale,
                "empty": t.empty,
            }
        )
    return pa.Table.from_pylist(rows, schema=SCHEMAS["test_economy"])


def _handler_json(handlers: Sequence[Handler]) -> str:
    return json.dumps(
        [{"line": h.line, "body_line": h.body_line, "types": list(h.types)} for h in handlers],
        separators=(",", ":"),
    )


def _run_economy_row(report: EconomyReport, computed_ms: float) -> dict[str, object]:
    row: dict[str, object] = asdict(report.totals)
    tax = report.tax
    row.update(
        {
            "base_run_id": tax.base_run_id if tax else None,
            "base_sha": tax.base_sha if tax else None,
            "base_exact": tax.base_exact if tax else None,
            "added_test_statements": tax.added_statements if tax else None,
            "removed_test_statements": tax.removed_statements if tax else None,
            "test_files_changed": tax.test_files_changed if tax else None,
            "newly_covered_lines": tax.newly_covered_lines if tax else None,
            "new_classes": tax.new_classes if tax else None,
            "new_tests": tax.new_tests if tax else None,
            "new_tests_unique_facts": tax.new_tests_unique_facts if tax else None,
            "test_tax": tax.tax if tax else None,
            "threshold": tax.threshold if tax else None,
            "min_statements": tax.min_statements if tax else None,
            "flagged": tax.flagged if tax else None,
            "tax_message": tax.message if tax else None,
            "weights_json": json.dumps(dict(report.weights), sort_keys=True),
            "notes_json": json.dumps(list(report.notes)),
            "computed_ms": round(computed_ms, 3),
        }
    )
    return row


def write_report(
    store: FactsStore, repo_id: int, run_id: str, report: EconomyReport, computed_ms: float
) -> None:
    """Write the four economy tables of one run from an :class:`EconomyReport`."""
    ids = store.dictionary(repo_id)

    def test_id(nodeid: str) -> int:
        found = ids.find_test_id(nodeid)
        if found is None:
            raise KeyError(f"test {nodeid!r} is not in the repo dictionary")
        return found

    store.write_table(repo_id, run_id, "test_economy", _test_economy_table(report, test_id))
    store.write_table(
        repo_id,
        run_id,
        "test_families",
        pa.Table.from_pylist(
            [
                {
                    "family": f.family,
                    "path": f.path,
                    "cases": f.cases,
                    "distinct_flows": f.distinct_flows,
                    "distinct_signatures": f.distinct_signatures,
                    "redundant_cases": f.redundant_cases,
                    "case_ids": [test_id(n) for n in f.case_ids],
                }
                for f in report.families
            ],
            schema=SCHEMAS["test_families"],
        ),
    )
    store.write_table(
        repo_id,
        run_id,
        "func_testing",
        pa.Table.from_pylist(
            [
                {
                    "func_key": f.func_key,
                    "path": f.path,
                    "qualname": f.qualname,
                    "kind": f.kind,
                    "covered": f.covered,
                    "observed": f.observed,
                    "direct_tests": f.direct_tests,
                    "indirect_tests": f.indirect_tests,
                    "indirect_only": f.indirect_only,
                    "untested": f.untested,
                    "handlers_total": f.handlers_total,
                    "handlers_unexercised": f.handlers_unexercised,
                    "handler_lines": _handler_json(f.handlers) if f.handlers_total is not None else None,
                }
                for f in report.functions
            ],
            schema=SCHEMAS["func_testing"],
        ),
    )
    store.write_table(
        repo_id,
        run_id,
        "run_economy",
        pa.Table.from_pylist([_run_economy_row(report, computed_ms)], schema=SCHEMAS["run_economy"]),
    )


def _too_big(store: FactsStore, ctx: RunContext) -> bool:
    """More ``test_lines`` rows than :data:`MAX_TEST_LINE_ROWS` in the shards' committed attempts."""
    latest: dict[int, tuple[int, int]] = {}
    for row in store.read_table(ctx.repo_id, ctx.run_id, "ingest_log").to_pylist():
        if row["status"] == "ok" and row["attempt"] >= latest.get(row["shard_id"], (0, 0))[0]:
            latest[row["shard_id"]] = (row["attempt"], row["n_test_lines"] or 0)
    return sum(rows for _, rows in latest.values()) > MAX_TEST_LINE_ROWS


def write_economy(
    store: FactsStore, ctx: RunContext, inputs: EconomyInputs | None = None
) -> EconomyReport | None:
    """Compute and write the economy tables of a run; never raises (the ingest must not fail).

    Called by ``finalize_run`` under the run lock. On failure the error is logged and, if possible,
    ``run_economy`` is written with only ``notes_json`` set, so the pages can say why.
    """
    if inputs is not None and not inputs.enabled:
        return None
    started = time.perf_counter()
    try:
        if _too_big(store, ctx):
            raise _Skip(f"economy skipped: more than {MAX_TEST_LINE_ROWS:,} per-test line rows")
        report = compute_economy(store, ctx, inputs)
        write_report(store, ctx.repo_id, ctx.run_id, report, (time.perf_counter() - started) * 1000.0)
        return report
    except Exception as exc:  # the economy is advisory: the ingest and the verdict must still land
        if not isinstance(exc, _Skip):
            log.exception("test economy of run %s failed", ctx.run_id)
        note = (
            str(exc)
            if isinstance(exc, _Skip)
            else redact(f"economy failed: {type(exc).__name__}: {exc}")[:300]
        )
        try:
            for table in ECONOMY_TABLES:
                store.delete_table(ctx.repo_id, ctx.run_id, table)
            row = {"notes_json": json.dumps([note]), "computed_ms": (time.perf_counter() - started) * 1000.0}
            store.write_table(
                ctx.repo_id,
                ctx.run_id,
                "run_economy",
                pa.Table.from_pylist([row], schema=SCHEMAS["run_economy"]),
            )
        except Exception:
            log.exception("recording the failed test economy of run %s failed", ctx.run_id)
        return None


class _Skip(Exception):
    """The economy was deliberately not computed (recorded as a note, not logged as an error)."""


# -- readers -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunEconomy:
    """The one ``run_economy`` row: run totals, the PR test tax and why parts were not measured.

    ``totals`` is None when the economy failed or was skipped (``notes`` says why).
    """

    run_id: str
    totals: RunTotals | None
    tax: TestTax | None
    notes: tuple[str, ...]
    weights: Mapping[str, int]
    computed_ms: float | None


@dataclass(frozen=True, slots=True)
class TestEconomyRow:
    """One test's economy row, with nodeids resolved (what the tables and cards show)."""

    __test__ = False

    nodeid: str
    path: str
    family: str | None
    measured: bool
    reason: str | None
    statements: int | None
    asserts: int | None
    helper_asserts: int | None
    raises: bool | None
    fixtures: int | None
    helper_statements: int | None
    file_lines: int | None
    cases: int
    lines: int
    arcs: int
    classes: int
    observations: int
    unique_lines: int
    unique_arcs: int
    unique_classes: int
    unique_facts: int
    leverage: float | None
    duration_ms: float | None
    cost_per_fact_ms: float | None
    signature: str
    group_id: str | None
    dominated_by: str | None
    in_minimal_set: bool
    removable: bool
    assertion_free: bool | None
    stale: bool | None
    empty: bool


@dataclass(frozen=True, slots=True)
class EquivalenceGroup:
    """Tests with identical signatures (same lines, arcs and shape observations)."""

    group_id: str
    nodeids: tuple[str, ...]
    duration_ms: float
    keep: str
    """The member the minimal set kept (else the fastest)."""


@dataclass(frozen=True, slots=True)
class Redundancy:
    """Equivalence groups, dominated tests and removable candidates of one run."""

    groups: tuple[EquivalenceGroup, ...]
    dominated: tuple[tuple[str, str], ...]
    """``(test, dominated by)`` pairs."""
    removable: tuple[TestEconomyRow, ...]
    """Removable candidates, slowest first."""
    minimal_set_size: int
    tests: int
    removable_duration_ms: float
    caveat: str = field(default=REMOVABLE_CAVEAT)


def _economy_rows(store: FactsStore, repo_id: int, run_id: str) -> list[TestEconomyRow]:
    table = store.read_table(repo_id, run_id, "test_economy")
    if table.num_rows == 0:
        return []
    ids = store.dictionary(repo_id)
    files = {r["nodeid"]: r["file_id"] for r in store.read_table(repo_id, run_id, "tests_dim").to_pylist()}
    rows: list[TestEconomyRow] = []
    for r in table.to_pylist():
        nodeid = ids.nodeid(r.pop("test_id"))
        dominated = r.pop("dominated_by")
        file_id = files.get(nodeid)
        bracket = nodeid.find("[")
        rows.append(
            TestEconomyRow(
                nodeid=nodeid,
                path=ids.path(file_id) if file_id is not None else nodeid.split("::", 1)[0],
                family=nodeid[:bracket] if bracket > 0 and nodeid.endswith("]") else None,
                dominated_by=ids.nodeid(dominated) if dominated is not None else None,
                **r,
            )
        )
    return rows


def run_economy(store: FactsStore, repo_id: int, run_id: str) -> RunEconomy | None:
    """The run's totals and tax, or None when the economy was never written for it."""
    rows = store.read_table(repo_id, run_id, "run_economy").to_pylist()
    if not rows:
        return None
    row = rows[0]
    notes = tuple(json.loads(row["notes_json"] or "[]"))
    weights = (
        json.loads(row["weights_json"])
        if row["weights_json"]
        else {
            "line": LINE_WEIGHT,
            "arc": ARC_WEIGHT,
            "class": CLASS_WEIGHT,
        }
    )
    totals = None
    if row["tests"] is not None:
        totals = RunTotals(**{name: row[name] for name in RunTotals.__dataclass_fields__})
    return RunEconomy(run_id, totals, _tax_from_row(row), notes, weights, row["computed_ms"])


def _tax_from_row(
    row: Mapping[str, object], threshold: float | None = None, min_statements: int | None = None
) -> TestTax | None:
    if row.get("test_tax") is None:
        return None
    added = int(row["added_test_statements"] or 0)  # type: ignore[call-overload]
    tax = float(row["test_tax"])  # type: ignore[arg-type]
    limit = float(row["threshold"]) if threshold is None else threshold  # type: ignore[arg-type]
    floor = int(row["min_statements"]) if min_statements is None else min_statements  # type: ignore[call-overload]
    lines = int(row["newly_covered_lines"] or 0)  # type: ignore[call-overload]
    classes = int(row["new_classes"] or 0)  # type: ignore[call-overload]
    return TestTax(
        base_run_id=row["base_run_id"],  # type: ignore[arg-type]
        base_sha=row["base_sha"],  # type: ignore[arg-type]
        base_exact=bool(row["base_exact"]),
        added_statements=added,
        removed_statements=int(row["removed_test_statements"] or 0),  # type: ignore[call-overload]
        test_files_changed=int(row["test_files_changed"] or 0),  # type: ignore[call-overload]
        newly_covered_lines=lines,
        new_classes=classes,
        new_tests=int(row["new_tests"] or 0),  # type: ignore[call-overload]
        new_tests_unique_facts=int(row["new_tests_unique_facts"] or 0),  # type: ignore[call-overload]
        tax=tax,
        threshold=limit,
        min_statements=floor,
        flagged=added >= floor and tax > limit,
        message=row["tax_message"] or tax_message(added, lines, classes),  # type: ignore[arg-type]
    )


def tests_economy(store: FactsStore, repo_id: int, run_id: str) -> list[TestEconomyRow]:
    """Every test's economy row, by node id (sorting and filters are the view model's:
    :func:`coverset.report.economy.sort_tests`)."""
    return sorted(_economy_rows(store, repo_id, run_id), key=lambda r: r.nodeid)


tests_economy.__test__ = False  # type: ignore[attr-defined]  # a reader, not a pytest test


def families(store: FactsStore, repo_id: int, run_id: str) -> list[FamilyEconomy]:
    """Parametrized families, most redundant cases first."""
    ids = store.dictionary(repo_id)
    found = [
        FamilyEconomy(
            family=r["family"],
            path=r["path"],
            cases=r["cases"],
            distinct_flows=r["distinct_flows"],
            distinct_signatures=r["distinct_signatures"],
            case_ids=tuple(ids.nodeid(t) for t in r["case_ids"] or ()),
        )
        for r in store.read_table(repo_id, run_id, "test_families").to_pylist()
    ]
    return sorted(found, key=lambda f: (-f.redundant_cases, -f.cases, f.family))


def func_testing(store: FactsStore, repo_id: int, run_id: str) -> list[FunctionTesting]:
    """How each production function is tested, by path and qualname."""
    rows: list[FunctionTesting] = []
    for r in store.read_table(repo_id, run_id, "func_testing").to_pylist():
        handlers = tuple(
            Handler(r["path"], h["line"], h["body_line"], tuple(h["types"]), False, r["func_key"])
            for h in json.loads(r["handler_lines"] or "[]")
        )
        rows.append(
            FunctionTesting(
                func_key=r["func_key"],
                path=r["path"],
                qualname=r["qualname"],
                kind=r["kind"],
                covered=bool(r["covered"]),
                observed=bool(r["observed"]),
                direct_tests=r["direct_tests"] or 0,
                indirect_tests=r["indirect_tests"] or 0,
                indirect_only=bool(r["indirect_only"]),
                untested=bool(r["untested"]),
                handlers_total=r["handlers_total"],
                handlers_unexercised=r["handlers_unexercised"],
                handlers=handlers,
            )
        )
    return sorted(rows, key=lambda f: (f.path, f.qualname))


def pr_test_tax(
    store: FactsStore,
    repo_id: int,
    run_id: str,
    *,
    threshold: float | None = None,
    min_statements: int | None = None,
) -> TestTax | None:
    """The run's PR test tax, re-flagged against ``threshold``/``min_statements`` when given (the
    effective ``policy.*`` values); None when the run had no base run to compare with."""
    rows = store.read_table(repo_id, run_id, "run_economy").to_pylist()
    return _tax_from_row(rows[0], threshold, min_statements) if rows else None
