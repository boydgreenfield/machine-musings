"""The test-economy analysis, as the agent tools read it.

:mod:`coverset.analysis.economy` measures what each test costs (statements, assertions, helpers,
fixtures, seconds) against what it alone covers (unique lines, arcs, input classes), finds
equivalence groups, dominated tests, collapsible parametrize families and the PR test tax. It runs
in two places, and this module reads either into one shape (:class:`EconomyData`):

* a facts-store run: the tables ``finalize_run`` wrote (:mod:`coverset.facts.economy`'s
  ``run_economy``, ``tests_economy``, ``redundancy``, ``families``, ``pr_test_tax``);
* a local bundle: :func:`coverset.analysis.economy.analyse` over the bundle, with the test files read
  from the work tree under the same rules as every other source (git-tracked files only).

When the analysis is absent (an older run, a build without it, a failure) the result is None with
the reason, and ``test_tax`` says so while ``redundant_tests`` falls back to the agent's own
grouping (:mod:`coverset.agent.cover`). Nothing here raises for missing data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from coverset.agent.context import RunContext

__all__ = ["EconomyData", "economy_for", "test_row"]

log = logging.getLogger(__name__)

_ROW_FIELDS = (
    "nodeid",
    "path",
    "family",
    "cases",
    "lines",
    "arcs",
    "classes",
    "unique_lines",
    "unique_arcs",
    "unique_classes",
    "unique_facts",
    "leverage",
    "duration_ms",
    "cost_per_fact_ms",
    "group_id",
    "dominated_by",
    "in_minimal_set",
    "removable",
    "assertion_free",
    "stale",
    "empty",
)
_WEIGHT_FIELDS = (
    "statements",
    "asserts",
    "helper_asserts",
    "raises",
    "fixtures",
    "helper_statements",
    "file_lines",
)


@dataclass(slots=True)
class EconomyData:
    """One run's economy in the agent's terms (either source)."""

    source: str
    totals: dict[str, Any] | None
    tax: dict[str, Any] | None
    tests: list[dict[str, Any]]
    groups: list[dict[str, Any]]
    dominated: list[tuple[str, str]]
    families: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)
    weights: dict[str, int] = field(default_factory=dict)
    caveat: str = ""


def test_row(obj: Any) -> dict[str, Any]:
    """A store ``TestEconomyRow`` or an in-memory ``TestEconomy`` as one flat dict."""
    out = {name: getattr(obj, name, None) for name in _ROW_FIELDS}
    weight = getattr(obj, "weight", None)
    source = weight if weight is not None else obj
    for name in _WEIGHT_FIELDS:
        out[name] = getattr(source, name, None)
    measured = getattr(obj, "measured", None)
    out["measured"] = bool(measured) if measured is not None else None
    reason = getattr(source, "reason", None)
    out["not_measured_reason"] = reason if out["measured"] is False else None
    return out


def _asdict(obj: Any) -> dict[str, Any] | None:
    if obj is None:
        return None
    slots = getattr(type(obj), "__dataclass_fields__", None)
    if not slots:
        return None
    return {name: getattr(obj, name) for name in slots}


def _families(found: Any) -> list[dict[str, Any]]:
    out = []
    for fam in found or ():
        redundant = getattr(fam, "redundant_cases", 0)
        if not redundant:
            continue
        out.append(
            {
                "family": fam.family,
                "path": fam.path,
                "cases": fam.cases,
                "distinct_flows": fam.distinct_flows,
                "redundant_cases": redundant,
            }
        )
    out.sort(key=lambda f: (-f["redundant_cases"], -f["cases"], f["family"]))
    return out


def economy_for(ctx: RunContext) -> tuple[EconomyData | None, str]:
    """The run's economy and, when it is None, why (cached on the context)."""
    if "economy" in ctx.cache:
        return ctx.cache["economy"]
    loader: Callable[[], tuple[EconomyData | None, str]] | None = ctx.cache.get("economy_loader")
    if loader is None:
        result: tuple[EconomyData | None, str] = (None, "this backend has no test-economy analysis")
    else:
        try:
            result = loader()
        except ImportError:
            result = (None, "the test-economy analysis is not part of this build of coverset")
        except Exception as exc:  # an analysis bug must not take the agent tools down with it
            log.exception("test economy failed for run %s", ctx.info.run_id)
            result = (None, f"the test-economy analysis failed ({type(exc).__name__})")
    ctx.cache["economy"] = result
    return result


def from_store(store: Any, repo_id: int, run_id: str) -> tuple[EconomyData | None, str]:
    """A facts-store run's economy tables (written at ``finalize_run``)."""
    from coverset.facts import economy as facts_economy

    run = facts_economy.run_economy(store, repo_id, run_id)
    if run is None:
        return (
            None,
            "the test economy was not computed for this run (it was finalized before the analysis existed)",
        )
    if run.totals is None:
        return None, "the test economy was skipped for this run: " + "; ".join(
            run.notes or ("no reason recorded",)
        )
    rows = facts_economy.tests_economy(store, repo_id, run_id, "leverage", None)
    found = facts_economy.redundancy(store, repo_id, run_id)
    groups = []
    dominated: list[tuple[str, str]] = []
    caveat = getattr(facts_economy, "REMOVABLE_CAVEAT", "")
    if found is not None:
        groups = [
            {"tests": list(g.nodeids), "keep": g.keep, "duration_ms": g.duration_ms} for g in found.groups
        ]
        dominated = [(a, b) for a, b in found.dominated]
        caveat = found.caveat
    return (
        EconomyData(
            source="facts store (computed when the run was finalized)",
            totals=_asdict(run.totals),
            tax=_asdict(run.tax),
            tests=[test_row(r) for r in rows],
            groups=groups,
            dominated=dominated,
            families=_families(facts_economy.families(store, repo_id, run_id)),
            notes=list(run.notes),
            weights=dict(run.weights),
            caveat=caveat,
        ),
        "",
    )


def from_bundle(ctx: RunContext, read: Callable[[str], str | None]) -> tuple[EconomyData | None, str]:
    """A local bundle's economy, computed now (no base run, so no PR test tax)."""
    from coverset.analysis import economy as analysis_economy

    bundle = getattr(ctx.facts, "bundle", None)
    if bundle is None:
        return None, "no bundle to analyse"
    report = analysis_economy.analyse(bundle, read, functions=list(ctx.facts.regions))
    by_group: dict[str, list[Any]] = {}
    for test in report.tests:
        if test.group_id is not None:
            by_group.setdefault(test.group_id, []).append(test)
    groups = []
    for members in by_group.values():
        members.sort(key=lambda t: t.nodeid)
        kept = [m for m in members if m.in_minimal_set]
        keep = kept[0] if kept else min(members, key=lambda m: (m.duration_ms or 0.0, m.nodeid))
        groups.append(
            {
                "tests": [m.nodeid for m in members],
                "keep": keep.nodeid,
                "duration_ms": round(sum(m.duration_ms or 0.0 for m in members), 3),
            }
        )
    groups.sort(key=lambda g: (-len(g["tests"]), g["tests"][0]))
    rows = [test_row(t) for t in report.tests]
    rows.sort(
        key=lambda r: (
            r["leverage"] is None,
            r["leverage"] if r["leverage"] is not None else 0.0,
            r["nodeid"],
        )
    )
    notes = list(report.notes)
    return (
        EconomyData(
            source="computed from the local bundle",
            totals=_asdict(report.totals),
            tax=_asdict(report.tax),
            tests=rows,
            groups=groups,
            dominated=sorted((t.nodeid, t.dominated_by) for t in report.tests if t.dominated_by),
            families=_families(report.families),
            notes=notes,
            weights=dict(report.weights),
            caveat=getattr(analysis_economy, "REMOVABLE_CAVEAT", ""),
        ),
        "",
    )
