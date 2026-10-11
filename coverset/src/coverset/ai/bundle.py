"""Build the redacted, size-capped :class:`~coverset.ai.schema.ReviewBundle` an AI reviewer sees.

Rules (docs/ai-review.md has the long form):

* Only facts go in: the function's post-change source and signature context, the diff hunks that
  touch it, its covering tests with their bodies, observed argument shapes, the deterministic
  input-class coverage from ``analysis.shapes``, the invariants in scope, and short oracle notes.
* Every string passes through :func:`coverset.logging.redact` (the shared credential regexes, the
  registered live secrets, and the settings' own secret values), once when it is read and once more
  over the finished bundle.
* Paths matching ``.env*``, ``*.pem``, ``*.key``, a ``secrets/`` directory, or any
  ``settings.ai_exclude_globs`` pattern (fnmatch, against the whole path and its basename) never
  appear: such functions are skipped and such tests are left out.
* Per-function caps truncate sources, test bodies and diff hunks; then the whole bundle is capped at
  ``settings.ai_max_bundle_chars`` characters of canonical JSON by dropping, in order: oracle notes,
  extra tests (down to one per function), long sources, long test bodies, long diff hunks, extra
  observed shapes, and finally whole functions from the end of the list. ``dropped`` counts it all.
* ``bundle_hash`` is the sha256 of the canonical JSON of what the reviewer sees: the bundle minus
  ``bundle_hash``, ``dropped`` and the run identifiers ``head_sha``/``pr_number``, so an unchanged
  function keeps its hash (and its stored finding) across pushes.
"""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, Final

from coverset.ai.schema import (
    CoveringTest,
    FunctionBundle,
    InputCovSummary,
    InvariantInScope,
    MissingClassHint,
    ObservedShape,
    ReviewBundle,
    ReviewContext,
)
from coverset.logging import redact

if TYPE_CHECKING:
    from coverset.analysis.invariants import Invariant
    from coverset.analysis.patch import FunctionRow
    from coverset.analysis.shapes import FunctionInputCov
    from coverset.config import Settings
    from coverset.lang.base import FunctionChange, FunctionRegion, ShapeObs

__all__ = [
    "ALWAYS_EXCLUDED_GLOBS",
    "LIMITS",
    "build_review_bundle",
    "bundle_hash",
    "canonical_json",
    "is_excluded_path",
]

ALWAYS_EXCLUDED_GLOBS: Final = (".env*", "*.pem", "*.key", "secrets/")
"""Paths never sent to a reviewer, whatever ``ai_exclude_globs`` says (a trailing ``/`` = directory)."""

LIMITS: Final = {
    "source_chars": 16_000,
    "signature_chars": 1_000,
    "diff_chars": 8_000,
    "test_body_chars": 4_000,
    "tests_per_function": 8,
    "shapes_per_function": 40,
    "shape_tests": 3,
    "example_chars": 200,
    "oracle_notes": 20,
    "oracle_note_chars": 300,
    "statement_chars": 2_000,
    "linked_tests": 20,
}
"""Per-function caps applied before the whole-bundle cap."""

_SOURCE_FLOOR: Final = 1_000
_TEST_BODY_FLOOR: Final = 500
_DIFF_FLOOR: Final = 1_000
_SHAPES_FLOOR: Final = 5
_SKIPPED_KINDS: Final = frozenset({"deleted", "trivial"})
_HUNK_RE: Final = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)
_SOURCE_ROOTS: Final = ("src/", "lib/")


# --- hashing -----------------------------------------------------------------------------------


def canonical_json(data: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 kept."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hashed_view(bundle: ReviewBundle) -> dict[str, Any]:
    data = bundle.model_dump(mode="json", exclude={"bundle_hash", "dropped"})
    data["context"].pop("head_sha", None)
    data["context"].pop("pr_number", None)
    return data


def bundle_hash(bundle: ReviewBundle) -> str:
    """sha256 hex of the canonical JSON of what the reviewer sees (see the module docstring)."""
    return hashlib.sha256(canonical_json(_hashed_view(bundle)).encode()).hexdigest()


# --- exclusion ---------------------------------------------------------------------------------


def is_excluded_path(path: str, extra_globs: Iterable[str] = ()) -> bool:
    """Whether ``path`` must never reach a reviewer (see :data:`ALWAYS_EXCLUDED_GLOBS`)."""
    norm = path.replace("\\", "/")
    while norm.startswith("./"):
        norm = norm[2:]
    parts = PurePosixPath(norm).parts
    name = parts[-1] if parts else norm
    for raw in (*ALWAYS_EXCLUDED_GLOBS, *extra_globs):
        pattern = raw.strip()
        if not pattern:
            continue
        if pattern.endswith("/"):
            directory = pattern.rstrip("/")
            if any(fnmatch.fnmatchcase(part, directory) for part in parts[:-1]) or fnmatch.fnmatchcase(
                norm, pattern + "*"
            ):
                return True
            continue
        if fnmatch.fnmatchcase(norm, pattern) or fnmatch.fnmatchcase(name, pattern):
            return True
    return False


def _nodeid_path(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


# --- text helpers ------------------------------------------------------------------------------


class _Redactor:
    """``redact`` with the settings' live secret values as extra secrets."""

    def __init__(self, settings: Settings) -> None:
        self._extra = tuple(settings.secret_values())

    def __call__(self, text: str | None) -> str:
        return redact(text or "", self._extra)


def _truncate(text: str, limit: int) -> tuple[str, int]:
    """Cut ``text`` to ``limit`` characters (marker included); return the text and chars removed."""
    if len(text) <= limit:
        return text, 0
    marker = f"\n# [truncated {len(text) - limit} chars]"
    keep = max(0, limit - len(marker))
    return text[:keep] + marker, len(text) - keep


def _lines(source: str, start: int, end: int) -> str:
    lines = source.splitlines()
    return "\n".join(lines[max(0, start - 1) : max(0, end)])


def _module_name(path: str, source_root: str | None) -> str | None:
    if not path.endswith(".py"):
        return None
    rel = path
    if source_root is not None:
        root = source_root.strip("/")
        if root and root != ".":
            if not rel.startswith(root + "/"):
                return None
            rel = rel[len(root) + 1 :]
    else:
        for prefix in _SOURCE_ROOTS:
            if rel.startswith(prefix):
                rel = rel[len(prefix) :]
                break
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) or None


def _signature_context(source: str, region: FunctionRegion) -> str:
    """Decorator and ``def`` lines, preceded by the enclosing ``class`` header for a method."""
    lines = source.splitlines()
    sig = lines[region.def_line - 1 : _header_end(lines, region)]
    if region.kind == "method" and region.def_line - 1 <= len(lines):
        indent = len(lines[region.def_line - 1]) - len(lines[region.def_line - 1].lstrip())
        for idx in range(region.def_line - 2, -1, -1):
            text = lines[idx]
            stripped = text.lstrip()
            if stripped.startswith("class ") and len(text) - len(stripped) < indent:
                sig = [text, "    ...", *sig]
                break
    return "\n".join(sig)


def _header_end(lines: Sequence[str], region: FunctionRegion) -> int:
    """1-based last line of the decorators and ``def`` header (before any docstring or body)."""
    chunk = _dedent("\n".join(lines[region.def_line - 1 : region.end_line]))
    try:
        tree = ast.parse(chunk)
    except (SyntaxError, ValueError):
        return region.def_line
    node = tree.body[0] if tree.body else None
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.body:
        return region.def_line
    first = node.body[0].lineno
    same_line = first == node.lineno  # one-line def: the header is the whole line
    return region.def_line - 1 + (first if same_line else first - 1)


def _function_hunks(diff: str, start: int, end: int) -> str:
    """The hunks of a unified diff whose new-side range overlaps ``[start, end]`` (all when unparsable)."""
    matches = list(_HUNK_RE.finditer(diff))
    if not matches:
        return diff
    picked: list[str] = []
    for i, match in enumerate(matches):
        new_start = int(match.group(1))
        new_len = int(match.group(2)) if match.group(2) is not None else 1
        new_end = new_start + max(new_len, 1) - 1
        if new_start <= end and new_end >= start:
            stop = matches[i + 1].start() if i + 1 < len(matches) else len(diff)
            picked.append(diff[match.start() : stop].rstrip("\n"))
    return "\n".join(picked)


def _called_names(source: str) -> set[str]:
    """Names called anywhere in ``source`` (``f(...)`` and ``mod.f(...)`` both give ``f``)."""
    try:
        tree = ast.parse(_dedent(source))
    except (SyntaxError, ValueError):
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _dedent(source: str) -> str:
    lines = source.splitlines()
    indents = [len(line) - len(line.lstrip()) for line in lines if line.strip()]
    cut = min(indents, default=0)
    return "\n".join(line[cut:] for line in lines)


def _docstring_raises(source: str) -> list[str]:
    try:
        tree = ast.parse(_dedent(source))
    except (SyntaxError, ValueError):
        return []
    node = tree.body[0] if tree.body else None
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return []
    doc = ast.get_docstring(node) or ""
    sentences = re.split(r"(?<=[.!?])\s+", " ".join(doc.split()))
    return [s for s in sentences if s.startswith(("Raises", "Raise "))]


def _asserts(body: str) -> bool:
    return any(token in body for token in ("assert", "pytest.raises", "pytest.approx", "self.assert"))


# --- building ----------------------------------------------------------------------------------


def build_review_bundle(
    *,
    repo_name: str,
    head_sha: str | None,
    pr_number: int | None,
    changes: Sequence[FunctionChange],
    sources: Mapping[str, str],
    diff_hunks: Mapping[str, str],
    patch_rows: Sequence[FunctionRow] | None,
    covering_tests: Mapping[str, Sequence[tuple[str, str]]],
    input_covs: Mapping[str, FunctionInputCov],
    shapes: Mapping[str, Sequence[ShapeObs]],
    invariants: Sequence[Invariant],
    settings: Settings,
    invariant_links: Mapping[str, Sequence[str]] | None = None,
    source_root: str | None = None,
    language: str = "python",
    conventions: Sequence[str] | None = None,
) -> ReviewBundle:
    """Build the review bundle for a run's changed functions (see the module docstring).

    ``sources`` and ``diff_hunks`` are keyed by path (head-side source text, unified diff text of
    that file). ``covering_tests`` maps a function's ``func_key`` (preferred) or qualname to
    ``(nodeid, test body)`` pairs in priority order. ``input_covs`` and ``shapes`` are keyed by
    ``func_key``. ``invariants`` is every invariant of the repo; those whose target is the function
    or a function it calls are included. ``invariant_links`` (invariant id → nodeids linked by
    marker or ``test_ids``) lets the bundle say which covering tests verify an invariant.
    ``source_root`` turns paths into module names (None: strip a leading ``src/`` or ``lib/``).
    """
    clean = _Redactor(settings)
    globs = tuple(settings.ai_exclude_globs)
    dropped: dict[str, int] = defaultdict(int)
    rows_by_key = {row.change.region.func_key: row for row in patch_rows or () if row.change.region}
    links = {k: set(v) for k, v in (invariant_links or {}).items()}
    targets = _invariant_targets(invariants)

    functions: list[FunctionBundle] = []
    seen: set[str] = set()
    for change in changes:
        region = change.region
        if region is None or change.kind in _SKIPPED_KINDS or region.kind not in ("function", "method"):
            dropped["skipped_functions"] += 1
            continue
        if region.func_key in seen:
            continue
        seen.add(region.func_key)
        if is_excluded_path(region.path, globs):
            dropped["excluded_paths"] += 1
            continue
        functions.append(
            _function_bundle(
                change.kind,
                region,
                source=sources.get(region.path),
                diff=diff_hunks.get(region.path, ""),
                row=rows_by_key.get(region.func_key),
                tests=covering_tests.get(region.func_key) or covering_tests.get(region.qualname) or (),
                cov=input_covs.get(region.func_key),
                observations=shapes.get(region.func_key, ()),
                invariants=invariants,
                targets=targets,
                links=links,
                source_root=source_root,
                clean=clean,
                globs=globs,
                dropped=dropped,
            )
        )

    context = ReviewContext(
        repo=clean(repo_name),
        head_sha=clean(head_sha) if head_sha else None,
        pr_number=pr_number,
        language=clean(language),
        conventions=[clean(c) for c in (conventions if conventions is not None else _conventions(functions))],
    )
    bundle = ReviewBundle(context=context, functions=functions)
    _apply_total_cap(bundle, settings.ai_max_bundle_chars, dropped)
    bundle = ReviewBundle.model_validate(_redact_tree(bundle.model_dump(mode="json"), clean))
    bundle.dropped = {k: v for k, v in sorted(dropped.items()) if v}
    bundle.bundle_hash = bundle_hash(bundle)
    return bundle


def _function_bundle(
    change_kind: str,
    region: FunctionRegion,
    *,
    source: str | None,
    diff: str,
    row: FunctionRow | None,
    tests: Sequence[tuple[str, str]],
    cov: FunctionInputCov | None,
    observations: Sequence[ShapeObs],
    invariants: Sequence[Invariant],
    targets: Mapping[str, list[tuple[Invariant, str]]],
    links: Mapping[str, set[str]],
    source_root: str | None,
    clean: _Redactor,
    globs: Sequence[str],
    dropped: dict[str, int],
) -> FunctionBundle:
    module = _module_name(region.path, source_root)
    raw_source = _lines(source, region.def_line, region.end_line) if source else ""
    body, cut = _truncate(clean(raw_source), LIMITS["source_chars"])
    dropped["source_chars"] += cut
    signature, cut = _truncate(
        clean(_signature_context(source, region) if source else ""), LIMITS["signature_chars"]
    )
    dropped["source_chars"] += cut
    hunk, cut = _truncate(
        clean(_function_hunks(diff, region.def_line, region.end_line) if diff else ""), LIMITS["diff_chars"]
    )
    dropped["diff_chars"] += cut

    covering, test_count = _covering_tests(tests, row, clean, globs, dropped)
    covering_ids = {t.nodeid for t in covering} | set(row.tests if row else ())
    in_scope = _invariants_in_scope(region, module, raw_source, targets, links, covering_ids, clean)
    return FunctionBundle(
        qualname=clean(region.qualname),
        func_key=region.func_key,
        path=clean(region.path),
        module=module,
        change_kind=change_kind,
        body_hash=region.body_hash,
        params=[clean(p) for p in region.params],
        signature_context=signature,
        source=body,
        diff_hunk=hunk,
        line_pct=row.line_pct if row else None,
        covering_test_count=test_count,
        covering_tests=covering,
        observed_shapes=_observed_shapes(observations, clean, globs, dropped),
        input_cov=_input_cov_summary(cov, clean) if cov is not None else None,
        invariants=in_scope,
        oracle_notes=_oracle_notes(raw_source, covering, in_scope, invariants, clean, dropped),
    )


def _covering_tests(
    tests: Sequence[tuple[str, str]],
    row: FunctionRow | None,
    clean: _Redactor,
    globs: Sequence[str],
    dropped: dict[str, int],
) -> tuple[list[CoveringTest], int]:
    pairs: dict[str, str] = {}
    for nodeid, body in tests:
        pairs.setdefault(nodeid, body or "")
    for nodeid in row.tests if row else ():
        pairs.setdefault(nodeid, "")
    kept: list[CoveringTest] = []
    for nodeid, body in pairs.items():
        if is_excluded_path(_nodeid_path(nodeid), globs):
            dropped["excluded_paths"] += 1
            continue
        if len(kept) >= LIMITS["tests_per_function"]:
            dropped["tests"] += 1
            continue
        text, cut = _truncate(clean(body), LIMITS["test_body_chars"])
        dropped["test_body_chars"] += cut
        kept.append(CoveringTest(nodeid=clean(nodeid), body=text))
    count = sum(1 for nodeid in pairs if not is_excluded_path(_nodeid_path(nodeid), globs))
    return kept, count


def _observed_shapes(
    observations: Sequence[ShapeObs], clean: _Redactor, globs: Sequence[str], dropped: dict[str, int]
) -> list[ObservedShape]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[str] = []
    for obs in observations:
        if obs.param not in order:
            order.append(obs.param)
        entry = merged.setdefault((obs.param, obs.shape_key), {"samples": 0, "example": None, "tests": []})
        entry["samples"] += obs.samples
        if entry["example"] is None and obs.example is not None:
            entry["example"] = obs.example
        for test in obs.tests:
            if test not in entry["tests"] and not is_excluded_path(_nodeid_path(test), globs):
                entry["tests"].append(test)
    rows = sorted(merged.items(), key=lambda kv: (order.index(kv[0][0]), -kv[1]["samples"], kv[0][1]))
    shapes: list[ObservedShape] = []
    for (param, key), entry in rows:
        if len(shapes) >= LIMITS["shapes_per_function"]:
            dropped["shapes"] += 1
            continue
        example = None
        if entry["example"] is not None:
            example, _ = _truncate(clean(entry["example"]), LIMITS["example_chars"])
        shapes.append(
            ObservedShape(
                param=clean(param),
                shape_key=clean(key),
                samples=entry["samples"],
                example=example,
                tests=[clean(t) for t in sorted(entry["tests"])[: LIMITS["shape_tests"]]],
            )
        )
    return shapes


def _input_cov_summary(cov: FunctionInputCov, clean: _Redactor) -> InputCovSummary:
    unannotated = [part.strip() for part in cov.note.split(";") if " unannotated: " in part]
    return InputCovSummary(
        expected=[clean(c.name) for c in cov.expected],
        observed=sorted(clean(name) for name in cov.observed),
        missing=[
            MissingClassHint(
                name=clean(c.name),
                family=clean(c.family),
                description=clean(c.description),
                hypothesis_hint=clean(c.hypothesis_hint),
            )
            for c in cov.missing
        ],
        missing_reason=clean(cov.missing_reason),
        unannotated=[clean(u) for u in unannotated],
        pct=cov.pct,
    )


def _invariant_targets(invariants: Sequence[Invariant]) -> dict[str, list[tuple[Invariant, str]]]:
    """Index invariants by the last component of their target (``allocate_cents``) for call matching."""
    index: dict[str, list[tuple[Invariant, str]]] = defaultdict(list)
    for inv in invariants:
        if inv.scope.kind in ("function", "class", "module"):
            index[inv.scope.target.rsplit(".", 1)[-1]].append((inv, inv.scope.target))
    return index


def _invariants_in_scope(
    region: FunctionRegion,
    module: str | None,
    source: str,
    targets: Mapping[str, list[tuple[Invariant, str]]],
    links: Mapping[str, set[str]],
    covering_ids: set[str],
    clean: _Redactor,
) -> list[InvariantInScope]:
    dotted = f"{module}.{region.qualname}" if module else None
    found: dict[str, tuple[Invariant, str]] = {}
    for candidates in targets.values():
        for inv, target in candidates:
            kind = inv.scope.kind
            if (
                (kind == "function" and dotted == target)
                or (kind == "class" and dotted is not None and dotted.startswith(target + "."))
                or (kind == "module" and module == target)
            ):
                found[inv.id] = (inv, "target")
    for name in sorted(_called_names(source)):
        for inv, target in targets.get(name, ()):
            if inv.scope.kind == "function" and inv.id not in found and target != dotted:
                found[inv.id] = (inv, "calls_target")
    scoped: list[InvariantInScope] = []
    for inv_id in sorted(found):
        inv, relation = found[inv_id]
        statement, _ = _truncate(clean(inv.statement), LIMITS["statement_chars"])
        linked = sorted(covering_ids & links.get(inv.id, set()))[: LIMITS["linked_tests"]]
        scoped.append(
            InvariantInScope(
                id=clean(inv.id),
                statement=statement,
                kind=inv.scope.kind,
                target=clean(inv.scope.target),
                severity=inv.severity,
                verify=sorted({spec.kind for spec in inv.verify}),
                relation=relation,  # type: ignore[arg-type]
                linked_tests=[clean(t) for t in linked],
            )
        )
    return scoped


def _oracle_notes(
    source: str,
    covering: Sequence[CoveringTest],
    in_scope: Sequence[InvariantInScope],
    invariants: Sequence[Invariant],
    clean: _Redactor,
    dropped: dict[str, int],
) -> list[str]:
    by_id = {inv.id: inv for inv in invariants}
    notes: list[str] = []
    for sentence in _docstring_raises(source):
        notes.append(f"docstring: {sentence}")
    for test in covering:
        if test.body and not _asserts(test.body):
            notes.append(f"test {test.nodeid} has no assert or pytest.raises")
    for scoped in in_scope:
        inv = by_id.get(scoped.id)
        if inv is None:
            continue
        notes += [f"{inv.id} example: {e}" for e in inv.examples]
        notes += [f"{inv.id} counterexample: {c}" for c in inv.counterexamples]
    kept: list[str] = []
    for note in notes:
        if len(kept) >= LIMITS["oracle_notes"]:
            dropped["oracle_notes"] += 1
            continue
        kept.append(_truncate(clean(note), LIMITS["oracle_note_chars"])[0])
    return kept


def _conventions(functions: Sequence[FunctionBundle]) -> list[str]:
    bodies = " ".join(t.body for fn in functions for t in fn.covering_tests)
    found = ["tests are pytest functions; nodeids are pytest nodeids"]
    if "hypothesis" in bodies or "@given" in bodies:
        found.append("the test suite uses Hypothesis (from hypothesis import given, strategies as st)")
    if "pytest.mark.invariant" in bodies:
        found.append('tests link invariants with @pytest.mark.invariant("<id>")')
    return found


def _redact_tree(value: Any, clean: _Redactor) -> Any:
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, list):
        return [_redact_tree(v, clean) for v in value]
    if isinstance(value, dict):
        return {k: _redact_tree(v, clean) for k, v in value.items()}
    return value


# --- whole-bundle cap --------------------------------------------------------------------------


def _size(model: Any) -> int:
    return len(canonical_json(model.model_dump(mode="json")))


def _apply_total_cap(bundle: ReviewBundle, cap: int, dropped: dict[str, int]) -> None:
    """Drop lowest-priority content until the canonical JSON fits in ``cap`` characters."""
    sizes = [_size(fn) for fn in bundle.functions]
    shell = _size(bundle.model_copy(update={"functions": []}))

    def total() -> int:
        return shell + sum(sizes) + max(0, len(sizes) - 1)

    def refresh(i: int) -> None:
        sizes[i] = _size(bundle.functions[i])

    if total() <= cap:
        return
    # 1. oracle notes
    for i, fn in enumerate(bundle.functions):
        if fn.oracle_notes:
            dropped["oracle_notes"] += len(fn.oracle_notes)
            fn.oracle_notes = []
            refresh(i)
    # 2. extra tests, from the function with the most, last test first
    while total() > cap:
        i = max(range(len(sizes)), key=lambda k: len(bundle.functions[k].covering_tests), default=-1)
        if i < 0 or len(bundle.functions[i].covering_tests) <= 1:
            break
        bundle.functions[i].covering_tests.pop()
        dropped["tests"] += 1
        refresh(i)
    # 3-5. long sources, then test bodies, then diff hunks, longest first, halving to a floor
    _shrink_text(bundle, sizes, total, cap, dropped, "source", _SOURCE_FLOOR, "source_chars")
    _shrink_tests(bundle, sizes, total, cap, dropped)
    _shrink_text(bundle, sizes, total, cap, dropped, "diff_hunk", _DIFF_FLOOR, "diff_chars")
    # 6. observed shapes beyond a few per function
    for i, fn in enumerate(bundle.functions):
        if total() <= cap:
            break
        if len(fn.observed_shapes) > _SHAPES_FLOOR:
            dropped["shapes"] += len(fn.observed_shapes) - _SHAPES_FLOOR
            fn.observed_shapes = fn.observed_shapes[:_SHAPES_FLOOR]
            refresh(i)
    # 7. whole functions, from the end
    while total() > cap and bundle.functions:
        bundle.functions.pop()
        sizes.pop()
        dropped["functions"] += 1


def _shrink_text(
    bundle: ReviewBundle,
    sizes: list[int],
    total: Callable[[], int],
    cap: int,
    dropped: dict[str, int],
    attr: str,
    floor: int,
    counter: str,
) -> None:
    while total() > cap:
        candidates = [(len(getattr(fn, attr)), i) for i, fn in enumerate(bundle.functions)]
        longest, i = max(candidates, default=(0, -1))
        if i < 0 or longest <= floor:
            return
        fn = bundle.functions[i]
        text, cut = _truncate(getattr(fn, attr), max(floor, longest // 2))
        setattr(fn, attr, text)
        dropped[counter] += cut
        sizes[i] = _size(fn)


def _shrink_tests(
    bundle: ReviewBundle, sizes: list[int], total: Callable[[], int], cap: int, dropped: dict[str, int]
) -> None:
    while total() > cap:
        candidates = [
            (len(t.body), i, j)
            for i, fn in enumerate(bundle.functions)
            for j, t in enumerate(fn.covering_tests)
        ]
        longest, i, j = max(candidates, default=(0, -1, -1))
        if i < 0 or longest <= _TEST_BODY_FLOOR:
            return
        test = bundle.functions[i].covering_tests[j]
        test.body, cut = _truncate(test.body, max(_TEST_BODY_FLOOR, longest // 2))
        dropped["test_body_chars"] += cut
        sizes[i] = _size(bundle.functions[i])
