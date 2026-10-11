"""Which tests of the prior run touched the code a diff changes.

"Test T calls function F" means T's prior-run lines intersect F's body range ``[body_start,
end_line]`` in the prior run's own regions (matched by ``func_key``; head-side line numbers are
never intersected with prior coverage). A class contributes only its own class-body lines (not its
methods), and changed files contribute their module-level lines.

Every change is resolved against the prior run's regions, whatever its kind relative to the diff
base: the head region's ``func_key`` first, then the base-side one. The diff runs from the
merge-base while the prior run may be the PR's previous head, so a function the PR *added* in an
earlier push and edits again now is ``added`` here yet present, with coverage, in the prior run.
A resolved region is risky when the change deletes it or its ``(body_hash, sig_hash)`` differs from
the head region's; equal hashes mean nothing changed since the prior run, so it is skipped.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from coverset.analysis.functions import FunctionChange, FunctionRegion, own_lines
from coverset.lang.base import IMPORT_CONTEXT, PriorRun

__all__ = ["ImpactIndex", "line_ranges"]


@dataclass(frozen=True, slots=True)
class ImpactIndex:
    """Prior-run tests per changed region, plus tests touching module-level lines of changed files."""

    regions: dict[str, FunctionRegion] = field(default_factory=dict)
    """Changed regions as they were in the prior run, by ``func_key``."""
    tests_by_region: dict[str, frozenset[str]] = field(default_factory=dict)
    module_tests: frozenset[str] = frozenset()

    @classmethod
    def build(
        cls, prior: PriorRun | None, changed: Sequence[FunctionChange], changed_files: Collection[str] = ()
    ) -> ImpactIndex:
        """Resolve ``changed`` against ``prior`` and collect the tests that touched each region."""
        if prior is None:
            return cls()
        prior_regions = prior.functions()
        by_key = {r.func_key: r for r in prior_regions}
        by_path: dict[str, list[FunctionRegion]] = {}
        for region in prior_regions:
            by_path.setdefault(region.path, []).append(region)
        regions: dict[str, FunctionRegion] = {}
        for change in changed:
            found = _resolve(change, by_key)
            if found is not None:
                regions[found.func_key] = found
        tests_by_region = {
            key: _tests_in(prior, region.path, _body_ranges(region, by_path.get(region.path, [])))
            for key, region in regions.items()
        }
        paths = set(changed_files) | {c.prev.path for c in changed if c.prev is not None}
        module_tests: set[str] = set()
        for path in sorted(paths):
            module_tests |= _tests_in(prior, path, line_ranges(prior.module_level_lines(path)))
        return cls(regions, tests_by_region, frozenset(module_tests))

    def regions_touched_by(self, nodeid: str) -> list[FunctionRegion]:
        """Changed prior regions ``nodeid`` touched, in source order."""
        touched = [self.regions[k] for k, tests in self.tests_by_region.items() if nodeid in tests]
        return sorted(touched, key=lambda r: (r.path, r.def_line))

    def impacted(self, nodeid: str) -> bool:
        """Whether ``nodeid`` touched a changed region or a changed file's module-level lines."""
        return nodeid in self.module_tests or any(nodeid in t for t in self.tests_by_region.values())


def line_ranges(lines: Iterable[int]) -> list[tuple[int, int]]:
    """Collapse line numbers into sorted inclusive ``(start, end)`` runs."""
    ranges: list[tuple[int, int]] = []
    for line in sorted(set(lines)):
        if ranges and line == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], line)
        else:
            ranges.append((line, line))
    return ranges


def _resolve(change: FunctionChange, by_key: Mapping[str, FunctionRegion]) -> FunctionRegion | None:
    """The prior-run region ``change`` makes risky, or None (absent, module-level, or unchanged)."""
    keys = [side.func_key for side in (change.region, change.prev) if side is not None]
    found = next((by_key[k] for k in keys if k in by_key), None)
    if found is None or found.kind == "module":
        return None
    head = change.region
    if change.kind == "deleted" or head is None:
        return found
    if (found.body_hash, found.sig_hash) == (head.body_hash, head.sig_hash):
        return None
    return found


def _body_ranges(region: FunctionRegion, same_file: Sequence[FunctionRegion]) -> list[tuple[int, int]]:
    if region.kind == "class":
        body = own_lines(region, same_file) & set(range(region.body_start, region.end_line + 1))
        return line_ranges(body)
    return [(region.body_start, region.end_line)]


def _tests_in(prior: PriorRun, path: str, ranges: Iterable[tuple[int, int]]) -> frozenset[str]:
    found: set[str] = set()
    for start, end in ranges:
        found |= prior.tests_touching(path, start, end)
    found.discard(IMPORT_CONTEXT)
    return frozenset(found)
