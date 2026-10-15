"""From ``git diff`` to classified function changes.

:func:`run_git_diff` runs a zero-context, rename- and copy-detecting diff on a mirror,
:func:`parse_diff` turns it into per-file changed line sets, and :func:`changed_functions` indexes
both sides of every changed file and classifies each touched region:

``added`` / ``deleted``
    present on one side only.
``modified``
    touched, and its ``body_hash`` or ``sig_hash`` differs. Also used for a renamed-and-edited
    function found by the fuzzy pass, with ``prev`` pointing at the old definition.
``trivial``
    touched, but both hashes are equal (a comment- or formatting-only edit).
``renamed`` / ``moved``
    a vanished definition whose ``body_hash`` and ``sig_hash`` reappear under a new qualname in
    the same file (renamed) or in another file (moved), including every unchanged function of a
    renamed file. Same body with a new signature is ``modified`` with ``prev`` set.

``trivial``, ``renamed`` and ``moved`` changes are behaviour-preserving: reports label them
``inherited`` and do not count them as untested (copying the prior run's facts is designed, not
built). Head-side line numbers (``added``) are only ever intersected with head regions and
base-side numbers (``removed``) with base regions.

Pairing vanished with appeared definitions is deliberately conservative, because a wrong pairing
lets changed code carry an inherited label it did not earn: an exact body match under a different short name
needs a substantial body (at least :data:`MIN_MATCH_TOKENS` code tokens) and, for classes, the same
set of direct members. The fuzzy pass compares bodies as sequences of normalised code lines
(tokens joined, so comments and layout do not matter), only for same-arity pairs with substantial
bodies; above :data:`FUZZY_MAX_PAIRS` candidates it keeps only pairs in the same file or with the
same short name, and if those are still too many it is skipped (everything stays added/deleted),
so a huge generated or moved file cannot stall planning.
"""

from __future__ import annotations

import difflib
import io
import re
import subprocess
import tokenize
from bisect import bisect_left, bisect_right
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from unidiff import PatchSet

from coverset.analysis.functions import MODULE_QUALNAME, FunctionChange, FunctionRegion, own_lines

__all__ = [
    "FileDiff",
    "FileStatus",
    "FunctionIndexer",
    "GitDiffError",
    "changed_functions",
    "is_behaviour_preserving",
    "parse_diff",
    "run_git_diff",
    "validate_ref",
]

type FileStatus = Literal["added", "deleted", "modified", "renamed", "copied"]
type ChangeKind = Literal["added", "deleted", "modified", "trivial", "renamed", "moved"]

FUZZY_THRESHOLD: Final = 0.6
"""Minimum :class:`difflib.SequenceMatcher` ratio for a renamed-and-edited pair."""
MIN_MATCH_TOKENS: Final = 10
"""Code tokens a body needs before it may pair with a differently named definition."""
FUZZY_MAX_PAIRS: Final = 2000
"""Upper bound on token-sequence comparisons in the fuzzy pass."""

GIT_TIMEOUT_S: Final = 120
_SHA_RE: Final = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BEHAVIOUR_PRESERVING: Final = frozenset({"trivial", "renamed", "moved"})
_C_ESCAPES: Final = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
_OCTAL: Final = frozenset("01234567")


class GitDiffError(RuntimeError):
    """``git diff`` could not be run, or a ref was rejected before running it."""


class FunctionIndexer(Protocol):
    """The part of a language adapter that :func:`changed_functions` needs."""

    def index_functions(self, path: str, source: str) -> list[FunctionRegion]:
        """Index every function region of one source file."""
        ...


@dataclass(frozen=True, slots=True)
class FileDiff:
    """Changed lines of one file: ``added`` are head-side lines, ``removed`` base-side lines."""

    path: str
    status: FileStatus
    added: frozenset[int] = field(default_factory=frozenset)
    removed: frozenset[int] = field(default_factory=frozenset)
    previous_path: str | None = None
    """Base-side path for renamed and copied files, else None."""

    @property
    def base_path(self) -> str:
        """The path this file had on the base side."""
        return self.previous_path or self.path


def is_behaviour_preserving(change: FunctionChange) -> bool:
    """Whether ``change`` is behaviour-preserving (trivial, renamed or moved) and so labelled inherited."""
    return change.kind in _BEHAVIOUR_PRESERVING


def validate_ref(ref: str, *, repo: Path | None = None) -> str:
    """Return ``ref`` if it is a full hex SHA or a well-formed ref name, else raise GitDiffError.

    Names are checked with ``git check-ref-format --allow-onelevel`` and may not start with ``-``.
    """
    if _SHA_RE.fullmatch(ref):
        return ref
    if not ref or ref.startswith("-") or "\0" in ref:
        raise GitDiffError(f"invalid git ref: {ref!r}")
    proc = subprocess.run(
        ["git", "check-ref-format", "--allow-onelevel", ref],
        cwd=repo,
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
        check=False,
    )
    if proc.returncode != 0:
        raise GitDiffError(f"invalid git ref: {ref!r}")
    return ref


def run_git_diff(mirror_or_repo: Path, base: str, head: str) -> str:
    """Return the zero-context diff ``base...head`` (merge-base to head) of a repo or bare mirror."""
    rev = f"{validate_ref(base, repo=mirror_or_repo)}...{validate_ref(head, repo=mirror_or_repo)}"
    cmd = [
        "git",
        "-C",
        str(mirror_or_repo),
        "-c",
        "core.quotePath=false",
        "diff",
        "-M50%",
        "-C",
        "--diff-filter=ACDMR",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        "--src-prefix=a/",
        "--dst-prefix=b/",
        "--end-of-options",
        rev,
        "--",
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=GIT_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitDiffError(f"git diff failed: {exc}") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace").strip()[:500]
        raise GitDiffError(f"git diff {rev} failed: {stderr}")
    return proc.stdout.decode("utf-8", "surrogateescape")


def parse_diff(text: str) -> dict[str, FileDiff]:
    """Parse unified diff ``text`` into a :class:`FileDiff` per path (base path for deletions)."""
    result: dict[str, FileDiff] = {}
    for patched in PatchSet(io.StringIO(text)):
        added = {line.target_line_no for hunk in patched for line in hunk if line.is_added}
        removed = {line.source_line_no for hunk in patched for line in hunk if line.is_removed}
        source = _strip_prefix(_unquote_git_path(patched.source_file), "a/")
        target = _strip_prefix(_unquote_git_path(patched.target_file), "b/")
        status: FileStatus
        previous: str | None = None
        if patched.is_added_file:
            status, path = "added", target
        elif patched.is_removed_file:
            status, path = "deleted", source
        elif _is_copy(patched.patch_info):
            status, path, previous = "copied", target, source
        elif patched.is_rename or source != target:
            status, path, previous = "renamed", target, source
        else:
            status, path = "modified", target
        result[path] = FileDiff(
            path=path,
            status=status,
            added=frozenset(n for n in added if n is not None),
            removed=frozenset(n for n in removed if n is not None),
            previous_path=previous,
        )
    return result


def changed_functions(
    adapter: FunctionIndexer,
    base_tree: Mapping[str, str | None],
    head_tree: Mapping[str, str | None],
    diffs: Mapping[str, FileDiff],
) -> list[FunctionChange]:
    """Classify every function region of the changed files (see the module docstring).

    ``base_tree`` maps base-side paths and ``head_tree`` head-side paths to source text (None or
    absent when the file does not exist on that side). Files whose source is missing on both sides
    (binary or non-source files) produce no changes. The result is sorted by path and line.
    """
    changes: list[FunctionChange] = []
    vanished: list[_Side] = []
    appeared: list[_Side] = []
    for path in sorted(diffs):
        diff = diffs[path]
        # A copied file's source still exists unchanged, so its definitions count as new code.
        old_src = None if diff.status in ("added", "copied") else base_tree.get(diff.base_path)
        new_src = None if diff.status == "deleted" else head_tree.get(diff.path)
        if old_src is None and new_src is None:
            continue
        old = _FileSide.build(adapter, diff.base_path, old_src)
        new = _FileSide.build(adapter, diff.path, new_src)
        changes += _pair_regions(old, new, diff, vanished, appeared)
    changes += _match_vanished(vanished, appeared)
    changes.sort(key=_change_sort_key)
    return changes


@dataclass(slots=True)
class _FileSide:
    path: str
    source: str
    regions: list[FunctionRegion]
    _code_lines: list[int] | None = None
    _code_text: list[str] = field(default_factory=list)
    _code_counts: list[int] = field(default_factory=list)
    _text_lines: list[str] | None = None

    @classmethod
    def build(cls, adapter: FunctionIndexer, path: str, source: str | None) -> _FileSide | None:
        if source is None:
            return None
        return cls(path, source, adapter.index_functions(path, source))

    def code(self, start: int, end: int) -> tuple[tuple[str, ...], int]:
        """Normalised code lines (tokens joined, comments and layout dropped) on ``start..end``,
        and how many code tokens they hold."""
        if self._code_lines is None:
            self._code_lines = []
            for line, tokens in _code_tokens(self.source):
                self._code_lines.append(line)
                self._code_text.append(" ".join(tokens))
                self._code_counts.append(len(tokens))
        lo = bisect_left(self._code_lines, start)
        hi = bisect_right(self._code_lines, end)
        return tuple(self._code_text[lo:hi]), sum(self._code_counts[lo:hi])

    def has_text(self, lines: set[int]) -> bool:
        """Whether any of ``lines`` holds non-blank text."""
        if self._text_lines is None:
            self._text_lines = self.source.splitlines()
        text = self._text_lines
        return any(0 < n <= len(text) and text[n - 1].strip() for n in lines)


@dataclass(slots=True)
class _Side:
    region: FunctionRegion
    file: _FileSide
    _body: tuple[tuple[str, ...], int] | None = None

    @property
    def body_lines(self) -> tuple[str, ...]:
        """Normalised code lines of the region's body (what the fuzzy pass compares)."""
        return self._body_code()[0]

    @property
    def body_tokens(self) -> int:
        """Number of code tokens in the region's body."""
        return self._body_code()[1]

    @property
    def short_name(self) -> str:
        return self.region.qualname.rsplit(".", 1)[-1]

    def members(self) -> frozenset[str]:
        """Names of the definitions directly inside a class region (empty for functions)."""
        if self.region.kind != "class":
            return frozenset()
        prefix = self.region.qualname + "."
        return frozenset(
            r.qualname[len(prefix) :]
            for r in self.file.regions
            if r.qualname.startswith(prefix) and "." not in r.qualname[len(prefix) :]
        )

    def _body_code(self) -> tuple[tuple[str, ...], int]:
        if self._body is None:
            self._body = self.file.code(self.region.body_start, self.region.end_line)
        return self._body


def _pair_regions(
    old: _FileSide | None,
    new: _FileSide | None,
    diff: FileDiff,
    vanished: list[_Side],
    appeared: list[_Side],
) -> list[FunctionChange]:
    """Pair old and new regions of one file by ``(qualname, ordinal)`` and classify touched pairs."""
    old_by_name = {(r.qualname, r.ordinal): r for r in old.regions} if old else {}
    new_by_name = {(r.qualname, r.ordinal): r for r in new.regions} if new else {}
    moved_file = diff.previous_path is not None and diff.previous_path != diff.path
    changes: list[FunctionChange] = []
    for name, prev in old_by_name.items():
        region = new_by_name.get(name)
        if region is None:
            if old is not None:
                vanished.append(_Side(prev, old))
            continue
        assert old is not None and new is not None
        same = prev.body_hash == region.body_hash and prev.sig_hash == region.sig_hash
        if moved_file:
            changes.append(FunctionChange(region, prev, "moved" if same else "modified"))
            continue
        touched_new = own_lines(region, new.regions) & diff.added
        touched_old = own_lines(prev, old.regions) & diff.removed
        if not touched_new and not touched_old:
            continue
        if not same:
            changes.append(FunctionChange(region, prev, "modified"))
        elif _worth_reporting(region, new, touched_new) or _worth_reporting(prev, old, touched_old):
            changes.append(FunctionChange(region, prev, "trivial"))
    for name, region in new_by_name.items():
        if name not in old_by_name and new is not None:
            appeared.append(_Side(region, new))
    return changes


def _worth_reporting(region: FunctionRegion, side: _FileSide, touched: set[int]) -> bool:
    """A trivial change to a function always counts; to a class or module only on non-blank lines."""
    if not touched:
        return False
    return region.kind in ("function", "method") or side.has_text(touched)


def _match_vanished(vanished: list[_Side], appeared: list[_Side]) -> list[FunctionChange]:
    """Pair vanished and appeared definitions: exact body-hash matches, then a fuzzy pass."""
    changes: list[FunctionChange] = []
    remaining_old = [s for s in vanished if s.region.kind != "module"]
    remaining_new = [s for s in appeared if s.region.kind != "module"]
    by_body: dict[str, list[_Side]] = {}
    for new in remaining_new:
        by_body.setdefault(new.region.body_hash, []).append(new)
    matched_new: set[int] = set()
    unmatched_old: list[_Side] = []
    for old in sorted(remaining_old, key=lambda s: (s.region.path, s.region.def_line)):
        candidates = [
            new
            for new in by_body.get(old.region.body_hash, ())
            if id(new) not in matched_new and _exact_match(old, new)
        ]
        if not candidates:
            unmatched_old.append(old)
            continue
        best = min(candidates, key=lambda new: _exact_preference(old.region, new.region))
        matched_new.add(id(best))
        if best.region.sig_hash != old.region.sig_hash:
            kind: ChangeKind = "modified"  # same body, new defaults/decorators: not behaviour-preserving
        else:
            kind = "renamed" if best.region.path == old.region.path else "moved"
        changes.append(FunctionChange(best.region, old.region, kind))
    unmatched_new = [s for s in remaining_new if id(s) not in matched_new]
    used_old: set[int] = set()
    used_new: set[int] = set()
    for i, j in _fuzzy_pairs(unmatched_old, unmatched_new):
        used_old.add(i)
        used_new.add(j)
        changes.append(FunctionChange(unmatched_new[j].region, unmatched_old[i].region, "modified"))
    changes += [
        FunctionChange(None, s.region, "deleted") for i, s in enumerate(unmatched_old) if i not in used_old
    ]
    changes += [
        FunctionChange(s.region, None, "added") for j, s in enumerate(unmatched_new) if j not in used_new
    ]
    changes += [FunctionChange(None, s.region, "deleted") for s in vanished if s.region.kind == "module"]
    changes += [FunctionChange(s.region, None, "added") for s in appeared if s.region.kind == "module"]
    return changes


def _exact_match(old: _Side, new: _Side) -> bool:
    """Whether two definitions with equal ``body_hash`` may be paired as the same code.

    Classes must also have the same direct members (a class hash skips its methods). A different
    short name additionally needs a substantial body, so unrelated ``pass``/``...`` stubs or
    one-liners are never paired with each other.
    """
    if not _compatible(old.region, new.region):
        return False
    if old.members() != new.members():
        return False
    if old.short_name == new.short_name:
        return True
    return bool(old.members()) or old.body_tokens >= MIN_MATCH_TOKENS


def _fuzzy_pairs(old: Sequence[_Side], new: Sequence[_Side]) -> list[tuple[int, int]]:
    """Greedy best-ratio pairs ``(old index, new index)`` of renamed-and-edited definitions.

    Only same-arity, compatible pairs with substantial bodies are compared. When there are more
    than :data:`FUZZY_MAX_PAIRS` of them, only pairs in the same file or with the same short name
    are; when even those are too many, no fuzzy pairing happens at all.
    """
    candidates = _fuzzy_candidates(old, new)
    by_new: dict[int, list[int]] = {}
    for i, j in candidates:
        by_new.setdefault(j, []).append(i)
    scored: list[tuple[float, int, int]] = []
    matcher = difflib.SequenceMatcher(None, autojunk=False)
    for j, olds in by_new.items():
        matcher.set_seq2(new[j].body_lines)
        for i in olds:
            matcher.set_seq1(old[i].body_lines)
            if matcher.real_quick_ratio() < FUZZY_THRESHOLD or matcher.quick_ratio() < FUZZY_THRESHOLD:
                continue
            ratio = matcher.ratio()
            if ratio >= FUZZY_THRESHOLD:
                scored.append((-ratio, i, j))
    pairs: list[tuple[int, int]] = []
    used_old: set[int] = set()
    used_new: set[int] = set()
    for _, i, j in sorted(scored):
        if i not in used_old and j not in used_new:
            used_old.add(i)
            used_new.add(j)
            pairs.append((i, j))
    return pairs


def _fuzzy_candidates(old: Sequence[_Side], new: Sequence[_Side]) -> list[tuple[int, int]]:
    """Index pairs worth comparing, bucketed so that counting them never enumerates V x A pairs."""
    narrowings: tuple[tuple[Callable[[_Side], tuple[str, ...]], ...], ...] = (
        (lambda s: (),),
        (lambda s: ("path", s.region.path), lambda s: ("name", s.short_name)),
    )
    for level in narrowings:
        groupings = [(_buckets(old, extra), _buckets(new, extra)) for extra in level]
        total = sum(
            len(olds) * len(news.get(k, ())) for olds_by, news in groupings for k, olds in olds_by.items()
        )
        if total <= FUZZY_MAX_PAIRS:
            return sorted(
                {
                    (i, j)
                    for olds_by, news in groupings
                    for k, olds in olds_by.items()
                    for i in olds
                    for j in news.get(k, ())
                }
            )
    return []


def _buckets(
    sides: Sequence[_Side], extra: Callable[[_Side], tuple[str, ...]]
) -> dict[tuple[object, ...], list[int]]:
    """Indices of substantial definitions grouped by (is a class, arity, *extra)."""
    groups: dict[tuple[object, ...], list[int]] = {}
    for index, side in enumerate(sides):
        if side.body_tokens >= MIN_MATCH_TOKENS:
            key = (side.region.kind == "class", len(side.region.params), *extra(side))
            groups.setdefault(key, []).append(index)
    return groups


def _compatible(old: FunctionRegion, new: FunctionRegion) -> bool:
    """Functions and methods may turn into each other; classes only match classes."""
    return (old.kind == "class") == (new.kind == "class") and MODULE_QUALNAME not in (
        old.qualname,
        new.qualname,
    )


def _exact_preference(old: FunctionRegion, new: FunctionRegion) -> tuple[int, int, int, str, int]:
    """Prefer the same file, then the same short name, then equal signatures, then the nearest line."""
    return (
        int(new.path != old.path),
        int(new.qualname.rsplit(".", 1)[-1] != old.qualname.rsplit(".", 1)[-1]),
        int(new.sig_hash != old.sig_hash),
        new.path,
        abs(new.def_line - old.def_line),
    )


def _code_tokens(source: str) -> list[tuple[int, list[str]]]:
    """``(line, tokens)`` for every line of ``source`` holding code; comments and layout dropped."""
    skip = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
    try:
        by_line: dict[int, list[str]] = {}
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type not in skip:
                by_line.setdefault(tok.start[0], []).append(tok.string)
        return sorted(by_line.items())
    except (tokenize.TokenError, SyntaxError):
        return [(n, line.split()) for n, line in enumerate(source.splitlines(), 1) if line.strip()]


def _change_sort_key(change: FunctionChange) -> tuple[str, int, str, str]:
    region = change.region or change.prev
    assert region is not None
    return (region.path, region.def_line, region.qualname, change.kind)


def _unquote_git_path(name: str) -> str:
    """Decode a path git C-quoted (``"a/tab\\there.py"``); other names are returned unchanged.

    ``core.quotePath=false`` stops git escaping non-ASCII bytes, but a path with ``"``, ``\\``, a
    tab, a newline or another control character is still quoted, with C escapes and 3-digit octal
    byte escapes. Bytes that are not UTF-8 survive as surrogate escapes.
    """
    if len(name) < 2 or name[0] != '"' or name[-1] != '"':
        return name
    body = name[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        char = body[i]
        if char != "\\":
            out += char.encode("utf-8", "surrogateescape")
            i += 1
        elif body[i + 1 : i + 2] in _C_ESCAPES:
            out.append(_C_ESCAPES[body[i + 1]])
            i += 2
        elif len(digits := body[i + 1 : i + 4]) == 3 and set(digits) <= _OCTAL:
            out.append(int(digits, 8) & 0xFF)
            i += 4
        else:
            out += b"\\"
            i += 1
    return out.decode("utf-8", "surrogateescape")


def _strip_prefix(name: str, prefix: str) -> str:
    return name[len(prefix) :] if name.startswith(prefix) else name


def _is_copy(header: Sequence[str]) -> bool:
    return any(str(line).startswith("copy from ") for line in header)
