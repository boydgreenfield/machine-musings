"""The agent tools over any backend: one class, plain JSON-serialisable answers.

:class:`AgentService` implements every tool of :data:`coverset.agent.tools.TOOLS` over a
:class:`~coverset.agent.backends.Backend` (a local ``.coverset`` directory or the facts store).
The MCP server and the HTTP API both call :meth:`AgentService.call`, so the two surfaces answer
identically. Rules every tool follows:

* **Plain data, stable names.** Results are dicts of strings, numbers, booleans, lists and dicts;
  a function is always named ``path::qualname`` (``function``) with its ``func_key``; a test by its
  nodeid exactly as the runner wrote it.
* **Caps and pages.** No list holds more than :data:`~coverset.agent.tools.MAX_ROWS` rows; a list
  that was cut says so with ``total`` and ``next_cursor`` (pass it back as ``cursor``).
* **Redaction.** Example values are the tracer's redacted reprs, scrubbed once more here (email
  local parts, credential shapes) and capped; failure messages are scrubbed and cut too. Nothing
  here reads a raw argument value: the facts never stored one.
* **Honesty.** Results that suggest removing tests are candidates, not verdicts; input classes come
  from sampled shapes; arcs per test are approximated. Each result carries the caveats that apply
  (``caveats``), so an agent that quotes a result quotes its limits too.

Heavy work (loading facts, building views, the cover index) runs in a worker thread through
``asyncio.to_thread``; the backend caches contexts per run, so repeated calls are cheap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from coverset.agent import cover
from coverset.agent.backends import Backend
from coverset.agent.context import RunContext
from coverset.agent.explain import explain_shape_key, fast_check_hint
from coverset.agent.tools import (
    CANDIDATES_NOT_VERDICTS,
    MAX_ROWS,
    REDACTED,
    SAMPLED,
    TOOLS,
    ToolError,
    ToolSpec,
    validate_args,
)
from coverset.analysis.shapes import example_sort_key
from coverset.lang.base import FunctionRegion
from coverset.report.economy import EconomyView, redundancy_of
from coverset.units import fmt_ranges
from coverset.web.facts_view import FunctionView, reached_classes

__all__ = ["HOW_TO_USE_EXAMPLES", "AgentService", "scrub_text"]

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS: Final = 2_000
MAX_EXAMPLE_CHARS: Final = 200
MAX_DESELECT: Final = 5_000
"""``deselect_args`` lists at most this many tests (the structured rows page at MAX_ROWS)."""
EXAMPLES_PER_CLASS: Final = 5
ARC_CAVEAT: Final = (
    "Per-test branch arcs are not recorded, only the run's union: an arc a->b counts for the tests "
    "that ran both lines (an exit arc for those that ran a and none of its other destinations)."
)
_TAX_SORTS: Final = {
    "leverage": ("leverage", False),
    "cost": ("cost_per_fact_ms", True),
    "statements": ("statements", True),
    "unique": ("unique_facts", False),
    "duration": ("duration_ms", True),
}
"""``test_tax``'s ``sort``: column and whether descending (leverage ascending: costliest first)."""
HOW_TO_USE_EXAMPLES: Final = (
    "These are redacted shapes, not raw data: the tracer kept a short repr of a value some test "
    "passed (email local parts masked as a***@domain, secret-named parameters hidden, strings cut at "
    "48 characters). Use them to see what kind of value each input class means and to write fixtures "
    "of the same shape; do not copy them as if they were real records, and never try to recover the "
    "hidden parts. For classes no test passed in, generate values with the given strategy instead."
)


def scrub_text(text: str | None, cap: int = MAX_EXAMPLE_CHARS) -> str | None:
    """``text`` with credential shapes and email local parts masked, cut at ``cap`` characters.

    Example reprs were redacted when captured; this is the second pass every string an agent sees
    goes through (the plugin's :func:`~pytest_coverset.redact.scrub` and the control plane's
    :func:`~coverset.logging.redact`), so a bundle from an older or foreign producer is held to the
    same rule."""
    if text is None:
        return None
    from coverset.logging import redact
    from pytest_coverset.redact import scrub

    cleaned = redact(scrub(text))
    if len(cleaned) > cap:
        cleaned = cleaned[: max(0, cap - 1)] + "…"
    return cleaned


_GIVEN = re.compile(r"^\s*@(?:\w+\.)*(?:given|example|settings)\b")
_DEF = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)\s*\(")


def property_tests(ctx: RunContext) -> frozenset[str]:
    """Nodeids of property-based tests (cached on the context).

    A neutral bundle says so itself (``property`` in ``tests.jsonl``: Hypothesis for pytest-coverset,
    fast-check for coverset-vitest). A facts-store run does not keep that flag, so the test files
    are read at the run's head and a test counts when a ``@given`` (or ``@example``/``@settings``)
    decorator sits on its ``def``."""
    found = ctx.cache.get("property_tests")
    if found is not None:
        return found
    bundle = getattr(ctx.facts, "bundle", None)
    names: set[str] = set()
    if bundle is not None and getattr(bundle, "collected", None):
        names = {c.nodeid for c in bundle.collected if getattr(c, "hypothesis", False)}
    else:
        by_file: dict[str, list[str]] = defaultdict(list)
        for nodeid in ctx.tests:
            by_file[nodeid.split("::", 1)[0]].append(nodeid)
        sources = ctx.sources([p for p in by_file if p.endswith(".py")])
        for path, text in sources.items():
            if not text or "given" not in text:
                continue
            decorated: set[str] = set()
            pending = False
            for line in text.splitlines():
                if _GIVEN.match(line):
                    pending = True
                    continue
                match = _DEF.match(line)
                if match:
                    if pending:
                        decorated.add(match.group(1))
                    pending = False
                elif (
                    line.strip()
                    and not line.lstrip().startswith(("@", "#", ")"))
                    and pending
                    and not line.startswith((" ", "\t"))
                ):
                    pending = False
            for nodeid in by_file[path]:
                if nodeid.split("::")[-1].split("[", 1)[0] in decorated:
                    names.add(nodeid)
    result = frozenset(names)
    ctx.cache["property_tests"] = result
    return result


def _fn(region: FunctionRegion) -> str:
    return f"{region.path}::{region.qualname}"


def _offset(cursor: str | None) -> int:
    if not cursor:
        return 0
    if not cursor.isdigit() or len(cursor) > 9:
        raise ToolError("invalid_argument", "that cursor did not come from this tool")
    return int(cursor)


def _page[T](items: Sequence[T], limit: int, cursor: str | None) -> tuple[list[T], dict[str, Any]]:
    """One page of ``items`` and its paging fields (``total``, ``next_cursor``)."""
    start = _offset(cursor)
    page = list(items[start : start + min(limit, MAX_ROWS)])
    end = start + len(page)
    return page, {"total": len(items), "next_cursor": str(end) if end < len(items) else None}


def _pct(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _fields(obj: Any) -> dict[str, Any] | None:
    """A dataclass's fields as a flat dict (None stays None)."""
    return None if obj is None else {name: getattr(obj, name) for name in obj.__dataclass_fields__}


def economy_for(ctx: RunContext) -> tuple[EconomyView | None, str]:
    """The run's economy (:mod:`coverset.report.economy`, the view every surface reads) through the
    backend's ``economy_loader``, or None and why; cached on the context. Never raises."""
    if "economy" in ctx.cache:
        return ctx.cache["economy"]
    loader: Callable[[], EconomyView | None] | None = ctx.cache.get("economy_loader")
    result: tuple[EconomyView | None, str]
    if loader is None:
        result = (None, "this backend has no test-economy analysis")
    else:
        try:
            view = loader()
        except Exception as exc:  # an analysis bug must not take the agent tools down with it
            log.exception("test economy failed for run %s", ctx.info.run_id)
            result = (None, f"the test-economy analysis failed ({type(exc).__name__})")
        else:
            if view is None:
                why = "the test economy was not computed for this run (it was finalized before the analysis existed)"
                result = (None, why)
            elif view.totals is None:
                result = (
                    None,
                    "the test economy was skipped for this run: "
                    + "; ".join(view.notes or ("no reason recorded",)),
                )
            else:
                result = (view, "")
    ctx.cache["economy"] = result
    return result


def _economy_source(view: EconomyView) -> str:
    return (
        "computed from the local bundle"
        if view.run_id == "bundle"
        else "facts store (computed when the run was finalized)"
    )


class AgentService:
    """Every agent tool over one backend (see the module docstring)."""

    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self._handlers: dict[str, Callable[..., Any]] = {
            name: getattr(self, f"tool_{name}") for name in TOOLS
        }

    @property
    def label(self) -> str:
        """Where the runs come from (the backend's label)."""
        return self.backend.label

    async def tools(self) -> list[ToolSpec]:
        """Every tool this service answers (the MCP ``tools/list``)."""
        return list(TOOLS.values())

    async def call(self, name: str, args: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Validate ``args`` for tool ``name`` and run it; raises :class:`ToolError`."""
        clean = validate_args(name, args)
        try:
            return await self._handlers[name](**clean)
        except ToolError:
            raise
        except Exception as exc:
            log.exception("agent tool %s failed", name)
            raise ToolError("internal", f"{name} failed: {type(exc).__name__}") from exc

    async def _ctx(self, run: str | None, repo: str | None) -> RunContext:
        return await self.backend.context(run, repo)

    @staticmethod
    def _head(ctx: RunContext) -> dict[str, Any]:
        info = ctx.info
        return {"run": info.run_id, "repo": info.repo}

    # -- repositories and runs -----------------------------------------------------------------------

    async def tool_list_repos(self) -> dict[str, Any]:
        """``list_repos``."""
        repos = await self.backend.list_repos()
        return {"backend": self.backend.label, "repos": repos[:MAX_ROWS], "total": len(repos)}

    async def tool_list_runs(self, repo: str | None, kind: str, limit: int) -> dict[str, Any]:
        """``list_runs``."""
        runs = await self.backend.list_runs(repo, kind, min(limit, MAX_ROWS))
        return {"repo": repo, "kind": kind, "runs": runs}

    # -- run summary -------------------------------------------------------------------------------------

    async def tool_get_run_summary(self, run: str | None, repo: str | None) -> dict[str, Any]:
        """``get_run_summary``."""
        ctx = await self._ctx(run, repo)
        return await asyncio.to_thread(self._run_summary, ctx)

    def _run_summary(self, ctx: RunContext) -> dict[str, Any]:
        facts = ctx.facts
        statements = covered = branches = taken = 0
        executed = facts.union_lines()
        for path, static in facts.statics.items():
            body = set(static.statements) - set(static.excluded)
            statements += len(body)
            covered += len(body & executed.get(path, set()))
            branch = facts.branch_lines.get(path, frozenset())
            possible = [arc for arc in static.arcs_possible if arc[0] in branch]
            branches += len(possible)
            ran = facts.run_arcs.get(path, frozenset())
            taken += sum(1 for arc in possible if arc in ran)
        outcomes = Counter(t.outcome or "not run" for t in ctx.tests.values())
        timed = [t for t in ctx.tests.values() if t.duration_s is not None]
        slowest = sorted(timed, key=lambda t: (-(t.duration_s or 0.0), t.nodeid))[:5]
        rows = self._gap_rows(ctx)
        called = [r for r in rows if r["called"]]
        expected = sum(r["expected"] for r in called)
        observed = sum(r["observed"] for r in called)
        out: dict[str, Any] = {
            **self._head(ctx),
            "run_info": ctx.info.to_json(),
            "language": ctx.language,
            "tests": {
                "total": len(ctx.tests),
                "outcomes": dict(sorted(outcomes.items())),
                "duration_s": round(sum(t.duration_s or 0.0 for t in timed), 3),
                "slowest": [{"nodeid": t.nodeid, "duration_s": t.duration_s} for t in slowest],
            },
            "coverage": {
                "files": len(facts.statics),
                "statements": statements,
                "covered": covered,
                "line_pct": _pct(covered, statements),
                "branches": branches,
                "branches_covered": taken,
                "branch_pct": _pct(taken, branches),
            },
            "input_classes": {
                "functions": len(rows),
                "functions_called": len(called),
                "functions_with_gaps": sum(1 for r in called if r["missing"]),
                "expected": expected,
                "observed": observed,
                "pct": _pct(observed, expected),
                "sampled_functions": sum(1 for r in called if r["tests_truncated"]),
            },
            "patch": None,
            "caveats": [SAMPLED, *ctx.cache.get("notes", [])],
        }
        changes = ctx.changes(None) if ctx.info.kind == "pr" else None
        if changes is not None and changes.patch is not None:
            patch = changes.patch
            out["patch"] = {
                "patch_pct": round(patch.patch_pct, 1) if patch.patch_pct is not None else None,
                "covered": patch.covered,
                "total": patch.total,
                "changed_functions": len(patch.function_rows),
                "zero_test_functions": [
                    _fn(r.change.region) for r in patch.zero_test_functions if r.change.region
                ][:MAX_ROWS],
                "complete": changes.complete,
            }
        steps = ["find_gaps: where a new test buys the most input classes"]
        if out["patch"] is not None:
            steps.insert(0, "get_patch_coverage: the changed lines and functions no test ran")
        elif ctx.info.kind == "local":
            steps.insert(0, "get_patch_coverage with base='origin/main': check your change before pushing")
        if outcomes.get("failed") or outcomes.get("error"):
            failing = sorted(n for n, t in ctx.tests.items() if t.outcome in ("failed", "error"))[:3]
            steps.insert(0, f"get_test {failing[0]!r}: the failure and what the test ran")
        steps.append("minimal_test_set / redundant_tests: what the suite could shed (candidates only)")
        out["next_steps"] = steps
        return out

    # -- gap table -----------------------------------------------------------------------------------

    def _gap_rows(self, ctx: RunContext) -> list[dict[str, Any]]:
        """Per function: expected, observed and missing classes, tests called, body size (cached).

        A facts-store run that was finalized has them stored (``func_input_cov``, the numbers the
        overview's "largest gaps" shows); otherwise each function's view is computed, which is what
        the pages do for a run the store did not finalize."""
        found = ctx.cache.get("gap_rows")
        if found is not None:
            return found
        facts = ctx.facts
        stored = facts.input_cov_rows
        rows: list[dict[str, Any]] = []
        regions = ctx.callables
        if not stored:
            ctx.views(regions)
        for region in regions:
            static = facts.statics.get(region.path)
            body = 0
            if static is not None:
                live = set(static.statements) - set(static.excluded)
                body = sum(1 for n in live if region.body_start <= n <= region.end_line)
            if stored:
                row = stored.get(region.func_key)
                if row is None:
                    continue
                try:
                    decoded = json.loads(row.get("missing_json") or "{}")
                except ValueError:
                    decoded = {}
                missing = sorted(
                    {
                        str(n)
                        for names in (decoded.values() if isinstance(decoded, dict) else [])
                        for n in names
                    }
                )
                tests_called = int(row.get("tests_called") or 0)
                rows.append(
                    {
                        "region": region,
                        "expected": int(row.get("blocks_expected") or 0),
                        "observed": int(row.get("blocks_observed") or 0),
                        "missing": missing,
                        "tests_called": tests_called,
                        "tests_truncated": int(row.get("tests_truncated") or 0),
                        "called": tests_called > 0,
                        "statements": body,
                    }
                )
                continue
            view = ctx.view(region)
            called_by = facts.tests_in_range(region.path, region.body_start, region.end_line)
            rows.append(
                {
                    "region": region,
                    "expected": len(view.cov.expected),
                    "observed": view.cov.observed_expected,
                    "missing": [c.name for c in view.cov.missing],
                    "tests_called": len(called_by),
                    "tests_truncated": view.cov.tests_truncated,
                    "called": bool(called_by) or bool(view.observations),
                    "statements": body,
                }
            )
        ctx.cache["gap_rows"] = rows
        return rows

    async def tool_find_gaps(
        self,
        run: str | None,
        repo: str | None,
        sort: str,
        path: str | None,
        include_uncalled: bool,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """``find_gaps``."""
        ctx = await self._ctx(run, repo)

        def build() -> dict[str, Any]:
            rows = [
                r
                for r in self._gap_rows(ctx)
                if r["missing"]
                and (r["called"] or include_uncalled)
                and (path is None or r["region"].path.startswith(path))
            ]
            if sort == "ratio":
                rows.sort(
                    key=lambda r: (
                        r["observed"] / r["expected"] if r["expected"] else 1.0,
                        -len(r["missing"]),
                        _fn(r["region"]),
                    )
                )
            elif sort == "size":
                rows.sort(key=lambda r: (-r["statements"], -len(r["missing"]), _fn(r["region"])))
            else:
                rows.sort(key=lambda r: (-len(r["missing"]), -r["statements"], _fn(r["region"])))
            page, paging = _page(rows, limit, cursor)
            out_rows = []
            for r in page:
                region = r["region"]
                line_pct, branch_pct = ctx.facts.body_coverage(region)
                out_rows.append(
                    {
                        "function": _fn(region),
                        "func_key": region.func_key,
                        "path": region.path,
                        "qualname": region.qualname,
                        "def_line": region.def_line,
                        "expected": r["expected"],
                        "observed": r["observed"],
                        "missing_count": len(r["missing"]),
                        "missing": r["missing"][:50],
                        "input_pct": _pct(r["observed"], r["expected"]),
                        "tests_called": r["tests_called"],
                        "sampled": r["tests_truncated"] > 0,
                        "line_pct": line_pct,
                        "branch_pct": branch_pct,
                        "body_statements": r["statements"],
                    }
                )
            return {
                **self._head(ctx),
                "sort": sort,
                "gaps": out_rows,
                **paging,
                "caveats": [SAMPLED, "A missing class means never observed, not buggy."],
            }

        return await asyncio.to_thread(build)

    # -- functions -----------------------------------------------------------------------------------

    def resolve(self, ctx: RunContext, spec: str) -> FunctionRegion:
        """The function ``spec`` names (see the ``function`` argument's description)."""
        spec = spec.strip()
        regions = ctx.callables
        if len(spec) == 16 and all(c in "0123456789abcdef" for c in spec):
            found = ctx.by_key.get(spec)
            if found is not None:
                return found
        if "::" in spec:
            path, _, qualname = spec.partition("::")
            matches = [r for r in regions if r.path == path and r.qualname == qualname]
            if not matches:
                matches = [r for r in regions if _path_suffix(r.path, path) and r.qualname == qualname]
            return self._one(spec, matches)
        head, sep, tail = spec.rpartition(":")
        if sep and tail.isdigit() and head:
            path = _resolve_path(ctx.facts.statics, head)
            found = ctx.innermost(path, int(tail))
            if found is None:
                raise ToolError("not_found", f"no function of {path} holds line {tail}")
            return found
        matches = [r for r in regions if r.qualname == spec]
        if not matches:
            matches = [r for r in regions if r.qualname.endswith("." + spec)]
        if not matches:
            folded = spec.casefold()
            near = [_fn(r) for r in regions if folded in r.qualname.casefold()][:20]
            raise ToolError("not_found", f"no function named {spec!r}", {"similar": near})
        return self._one(spec, matches)

    @staticmethod
    def _one(spec: str, matches: list[FunctionRegion]) -> FunctionRegion:
        if not matches:
            raise ToolError("not_found", f"no function {spec!r}")
        if len(matches) > 1:
            raise ToolError(
                "ambiguous",
                f"{spec!r} names {len(matches)} functions: pass path::qualname or the func_key",
                {"candidates": [{"function": _fn(r), "func_key": r.func_key} for r in matches[:20]]},
            )
        return matches[0]

    async def _function(
        self, run: str | None, repo: str | None, function: str
    ) -> tuple[RunContext, FunctionView]:
        ctx = await self._ctx(run, repo)
        # resolve() reads ctx.callables, which loads the functions table on a cold context: keep
        # that off the event loop so one large run does not stall every other request.
        region = await asyncio.to_thread(self.resolve, ctx, function)
        view = await asyncio.to_thread(ctx.view, region)
        return ctx, view

    def _class_rows(self, view: FunctionView, *, param: str | None = None) -> list[dict[str, Any]]:
        cov = view.cov
        rows = []
        for cls in cov.expected:
            if param is not None and cls.param != param:
                continue
            observed = cls.name in cov.observed
            rows.append(
                {
                    "class": cls.name,
                    "param": cls.param,
                    "family": cls.family,
                    "description": cls.description,
                    "observed": observed,
                    "samples": int(view.samples.get(cls.name, 0)),
                    "example": scrub_text(cov.examples.get(cls.name)) if observed else None,
                    "tests": list(cov.tests.get(cls.name, ()))[:3] if observed else [],
                    "strategy": None if observed else cls.hypothesis_hint,
                }
            )
        return rows

    async def tool_get_function(
        self, run: str | None, repo: str | None, function: str, limit: int
    ) -> dict[str, Any]:
        """``get_function``."""
        ctx, view = await self._function(run, repo, function)
        return await asyncio.to_thread(self._function_detail, ctx, view, limit)

    def _function_detail(self, ctx: RunContext, view: FunctionView, limit: int) -> dict[str, Any]:
        region = view.region
        facts = ctx.facts
        lines = facts.line_facts(region.path)
        body = {n for n in lines.statements - lines.excluded if region.body_start <= n <= region.end_line}
        missing = sorted(body - set(lines.executed))
        partial = {
            str(line): [d if d > 0 else "exit" for d in dests]
            for line, dests in sorted(lines.partial.items())
            if region.body_start <= line <= region.end_line
        }
        covering = facts.tests_in_range(region.path, region.body_start, region.end_line)
        direct: set[str] = set()
        callers: dict[tuple[str, str, int], dict[str, Any]] = {}
        for obs in view.observations:
            if obs.param == "return":
                continue
            if isinstance(obs.caller, str):
                if obs.caller:
                    direct.add(obs.caller)
                continue
            path, qualname, line = obs.caller
            row = callers.setdefault(
                obs.caller, {"function": f"{path}::{qualname}", "line": line, "samples": 0, "tests": set()}
            )
            row["samples"] += obs.samples
            row["tests"].update(obs.tests)
        ordered = sorted(covering, key=lambda n: (ctx.duration(n) or float("inf"), n))
        tests = [
            {
                "nodeid": n,
                "outcome": ctx.tests[n].outcome if n in ctx.tests else None,
                "duration_s": ctx.duration(n),
                "direct": n in direct,
            }
            for n in ordered[: min(limit, MAX_ROWS)]
        ]
        cov = view.cov
        caveats = [SAMPLED]
        if cov.note:
            caveats.append(cov.note)
        if not view.observations:
            caveats.append(
                "No shape was recorded for this function: no test called it, or it was not traced."
            )
        return {
            **self._head(ctx),
            "function": _fn(region),
            "func_key": region.func_key,
            "path": region.path,
            "qualname": region.qualname,
            "kind": region.kind,
            "def_line": region.def_line,
            "body_start": region.body_start,
            "end_line": region.end_line,
            "params": list(region.params),
            "annotations": {k: v for k, v in view.annotations.items() if v is not None},
            "returns": view.returns,
            "coverage": {
                "line_pct": view.line_pct,
                "branch_pct": view.branch_pct,
                "body_statements": len(body),
                "missing_lines": fmt_ranges(missing)[:MAX_ROWS],
                "partial_branches": partial,
            },
            "input_classes": {
                "expected": len(cov.expected),
                "observed": cov.observed_expected,
                "pct": cov.pct,
                "missing_reason": cov.missing_reason,
                "classes": self._class_rows(view),
                "returns_seen": [c.name for c in view.return_cells],
            },
            "tests": {
                "total": len(covering),
                "direct": len(direct),
                "rows": tests,
                "truncated": len(covering) > len(tests),
            },
            "callers": [
                {
                    "function": c["function"],
                    "line": c["line"],
                    "samples": c["samples"],
                    "tests": len(c["tests"]),
                }
                for c in sorted(callers.values(), key=lambda c: (-c["samples"], c["function"]))
            ][: min(limit, MAX_ROWS)],
            "callers_total": len(callers),
            "caveats": caveats,
        }

    # -- patch coverage ----------------------------------------------------------------------------------

    async def tool_get_patch_coverage(
        self, run: str | None, repo: str | None, base: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        """``get_patch_coverage``."""
        ctx = await self._ctx(run, repo)
        local = ctx.info.kind == "local"
        if local and not base:
            return {
                **self._head(ctx),
                "applicable": False,
                "reason": "a local .coverset has no diff of its own: pass base (a git ref such as origin/main)",
            }
        changes = await asyncio.to_thread(ctx.changes, base if local else None)
        if changes is None:
            return {
                **self._head(ctx),
                "applicable": False,
                "reason": f"run {ctx.info.run_id} is a {ctx.info.kind} run, not a pull request: pick a PR run "
                "from list_runs(kind='pr')",
            }
        if changes.error:
            return {
                **self._head(ctx),
                "applicable": False,
                "reason": f"the diff could not be read: {changes.error}",
            }

        def build() -> dict[str, Any]:
            patch = changes.patch
            files = []
            if patch is not None:
                for path, fp in sorted(patch.per_file.items()):
                    files.append(
                        {
                            "path": path,
                            "covered": fp.covered,
                            "total": fp.total,
                            "pct": round(fp.pct, 1) if fp.pct is not None else None,
                            "missing_lines": fmt_ranges(fp.missing)[:MAX_ROWS],
                        }
                    )
            rows = list(patch.function_rows) if patch is not None else []
            page, paging = _page(rows, limit, cursor)
            functions = []
            for row in page:
                region = row.change.region
                if region is None:
                    continue
                view = ctx.view(region)
                functions.append(
                    {
                        "function": _fn(region),
                        "func_key": region.func_key,
                        "change": row.change.kind,
                        "inherited": row.inherited,
                        "line_pct": row.line_pct,
                        "tests": len(row.tests),
                        "tests_sample": sorted(row.tests, key=lambda n: (ctx.duration(n) or float("inf"), n))[
                            :5
                        ],
                        "input_classes": {
                            "expected": len(view.cov.expected),
                            "observed": view.cov.observed_expected,
                            "missing": [c.name for c in view.cov.missing],
                        },
                    }
                )
            measured = set(ctx.facts.statics)
            return {
                **self._head(ctx),
                "applicable": True,
                "base": changes.base,
                "head": changes.head,
                "patch_pct": round(patch.patch_pct, 1) if patch and patch.patch_pct is not None else None,
                "covered": patch.covered if patch else 0,
                "total": patch.total if patch else 0,
                "complete": changes.complete,
                "stale": changes.stale,
                "files": files[:MAX_ROWS],
                "unmeasured_files": sorted(p for p in changes.changed_lines if p not in measured)[:MAX_ROWS],
                "functions": functions,
                "functions_total": paging["total"],
                "next_cursor": paging["next_cursor"],
                "zero_test_functions": [
                    _fn(r.change.region)
                    for r in (patch.zero_test_functions if patch else [])
                    if r.change.region
                ][:MAX_ROWS],
                "indirect": [
                    {
                        "function": _fn(c.change.region or c.change.prev)
                        if (c.change.region or c.change.prev)
                        else None,
                        "change": c.change.kind,
                        "reason": c.reason,
                    }
                    for c in (patch.indirect if patch else [])
                ][:MAX_ROWS],
                "notes": changes.notes,
                "caveats": [SAMPLED],
            }

        return await asyncio.to_thread(build)

    # -- covering tests and one test -------------------------------------------------------------------

    async def tool_get_covering_tests(
        self,
        run: str | None,
        repo: str | None,
        path: str | None,
        line: int | None,
        function: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """``get_covering_tests``."""
        if not path and not function:
            raise ToolError("invalid_argument", "pass `path` (with or without `line`) or `function`")
        ctx = await self._ctx(run, repo)
        facts = ctx.facts
        import_time = None
        if function:
            region = await asyncio.to_thread(self.resolve, ctx, function)
            target: dict[str, Any] = {
                "function": _fn(region),
                "lines": f"{region.body_start}-{region.end_line}",
            }
            found = await asyncio.to_thread(
                facts.tests_in_range, region.path, region.body_start, region.end_line
            )
        else:
            assert path is not None
            path = _resolve_path(facts.statics, path)
            if line is not None:
                target = {"path": path, "line": line}
                found = await asyncio.to_thread(facts.tests_in_range, path, line, line)
                import_time = facts.import_covers(path, line)
            else:
                end = facts.statics[path].n_lines or 10**9
                target = {"path": path}
                found = await asyncio.to_thread(facts.tests_in_range, path, 1, end)
        ordered = sorted(found, key=lambda n: (ctx.duration(n) or float("inf"), n))
        page, paging = _page(ordered, limit, cursor)
        rows = [
            {
                "nodeid": n,
                "outcome": ctx.tests[n].outcome if n in ctx.tests else None,
                "duration_s": ctx.duration(n),
            }
            for n in page
        ]
        fastest = ordered[:10]
        return {
            **self._head(ctx),
            "target": target,
            "import_time": import_time,
            "tests": rows,
            **paging,
            "pytest_command": ("pytest " + " ".join(shlex.quote(n) for n in fastest)) if fastest else None,
        }

    async def tool_get_test(self, run: str | None, repo: str | None, nodeid: str) -> dict[str, Any]:
        """``get_test``."""
        ctx = await self._ctx(run, repo)
        # ctx.test_ids reads (and sorts) the whole per-test lines table on a cold context.
        known = await asyncio.to_thread(lambda: nodeid in ctx.tests or nodeid in ctx.test_ids)
        if not known:
            folded = nodeid.casefold()
            near = sorted(n for n in ctx.tests if folded in n.casefold())[:10]
            raise ToolError("not_found", "no such test in this run", {"similar": near})
        message, phase = await self.backend.failure_message(ctx, nodeid)
        out = await asyncio.to_thread(self._test_detail, ctx, nodeid)
        info = ctx.tests.get(nodeid)
        out["failed_phase"] = (info.failed_phase if info else None) or phase
        out["message"] = scrub_text(message or (info.message if info else None), MAX_MESSAGE_CHARS)
        return out

    def _test_detail(self, ctx: RunContext, nodeid: str) -> dict[str, Any]:
        info = ctx.tests.get(nodeid)
        lines = ctx.test_lines(nodeid)
        facts = ctx.facts
        files = []
        functions = []
        by_path: dict[str, list[FunctionRegion]] = defaultdict(list)
        for region in ctx.callables:
            by_path[region.path].append(region)
        for path, ran in lines.items():
            static = facts.statics.get(path)
            live = set(static.statements) - set(static.excluded) if static else set()
            files.append(
                {
                    "path": path,
                    "lines_run": len(ran),
                    "statements_run": len(live & set(ran)),
                    "lines": fmt_ranges(ran)[:50],
                }
            )
            owners: dict[str, list[int]] = defaultdict(list)
            for line in ran:
                holding = [r for r in by_path.get(path, ()) if r.body_start <= line <= r.end_line]
                if holding:
                    owner = max(holding, key=lambda r: (r.body_start, -r.end_line))
                    owners[owner.func_key].append(line)
            for key, owned in owners.items():
                region = ctx.by_key[key]
                body = sum(1 for n in live if region.body_start <= n <= region.end_line)
                run_body = sum(1 for n in owned if n in live)
                functions.append(
                    {
                        "function": _fn(region),
                        "func_key": key,
                        "statements_run": run_body,
                        "body_statements": body,
                        "pct": _pct(run_body, body),
                    }
                )
        shapes = []
        for key, observations in ctx.observations().items():
            mine = [o for o in observations if nodeid in o.tests or o.caller == nodeid]
            if not mine:
                continue
            region = ctx.by_key.get(key)
            if region is None:
                continue
            view = ctx.view(region)
            for obs in mine:
                reached = reached_classes(view, [obs])
                shapes.append(
                    {
                        "function": _fn(region),
                        "param": obs.param,
                        "shape_key": obs.shape_key,
                        "classes": sorted({c for found in reached.values() for c in found}),
                        "example": scrub_text(obs.example) if obs.example_test == nodeid else None,
                        "direct": obs.caller == nodeid,
                    }
                )
        shapes.sort(key=lambda s: (s["function"], s["param"], s["shape_key"]))
        history = None
        history_note = None
        if ctx.history_loader is not None:
            history = ctx.history_loader(nodeid, 10)
        else:
            history_note = "a local bundle has no history: run against a deployed coverset for it"
        return {
            **self._head(ctx),
            "nodeid": nodeid,
            "file": nodeid.split("::", 1)[0],
            "outcome": info.outcome if info else None,
            "duration_s": info.duration_s if info else None,
            "files": files[:MAX_ROWS],
            "files_total": len(files),
            "functions": sorted(functions, key=lambda f: f["function"])[:MAX_ROWS],
            "functions_total": len(functions),
            "shapes": shapes[:MAX_ROWS],
            "shapes_truncated": len(shapes) > MAX_ROWS,
            "history": history,
            "history_note": history_note,
            "caveats": [SAMPLED, REDACTED],
        }

    # -- examples and suggestions ------------------------------------------------------------------------

    async def tool_example_inputs(
        self, run: str | None, repo: str | None, function: str, param: str | None, limit: int
    ) -> dict[str, Any]:
        """``example_inputs``."""
        ctx, view = await self._function(run, repo, function)
        catalogue = view.catalogue
        expected = {c.name for c in view.cov.expected}
        by_class: dict[str, list[Any]] = defaultdict(list)
        returns: dict[str, list[Any]] = defaultdict(list)
        for obs in view.observations:
            if obs.example is None:
                continue
            if obs.param == "return":
                returns[obs.shape_key].append(obs)
                continue
            for name in catalogue.observed_classes([obs]) & expected:
                by_class[name].append(obs)
        classes = []
        for row in self._class_rows(view, param=None if param in (None, "return") else param):
            if param == "return":
                break
            found = sorted(
                by_class.get(row["class"], []), key=lambda o: example_sort_key(o.example, o.example_test)
            )
            seen: set[str] = set()
            examples = []
            for obs in found:
                text = scrub_text(obs.example)
                if text is None or text in seen:
                    continue
                seen.add(text)
                examples.append(
                    {
                        "repr": text,
                        "test": obs.example_test,
                        "shape_key": obs.shape_key,
                        "samples": obs.samples,
                        "caller": obs.caller
                        if isinstance(obs.caller, str)
                        else f"{obs.caller[0]}::{obs.caller[1]}",
                    }
                )
                if len(examples) >= EXAMPLES_PER_CLASS:
                    break
            classes.append(
                {
                    "class": row["class"],
                    "param": row["param"],
                    "description": row["description"],
                    "observed": row["observed"],
                    "examples": examples,
                    "strategy": row["strategy"],
                }
            )
        return_rows = []
        if param in (None, "return"):
            for key in sorted(returns):
                obs = sorted(returns[key], key=lambda o: example_sort_key(o.example, o.example_test))[0]
                return_rows.append(
                    {"shape_key": key, "repr": scrub_text(obs.example), "test": obs.example_test}
                )
        return {
            **self._head(ctx),
            "function": _fn(view.region),
            "func_key": view.region.func_key,
            "how_to_use": HOW_TO_USE_EXAMPLES,
            "classes": classes[: min(limit, MAX_ROWS)],
            "classes_truncated": len(classes) > min(limit, MAX_ROWS),
            "returns": return_rows[:MAX_ROWS],
            "caveats": [REDACTED, SAMPLED],
        }

    async def tool_suggest_tests(self, run: str | None, repo: str | None, function: str) -> dict[str, Any]:
        """``suggest_tests``."""
        ctx, view = await self._function(run, repo, function)
        from coverset.web.detail import hypothesis_stub

        typescript = ctx.language in ("typescript", "javascript")
        missing = []
        for cls in view.cov.missing:
            row = {
                "class": cls.name,
                "param": cls.param,
                "description": cls.description,
                "hypothesis": cls.hypothesis_hint,
            }
            if typescript:
                row["fast_check"] = fast_check_hint(cls.name, cls.hypothesis_hint)
            missing.append(row)
        if typescript:
            snippet = _fast_check_snippet(view, missing)
            framework = "fast-check"
            notes = [
                "TypeScript: fast_check is a direct translation where the catalogue defines one; otherwise "
                "translate the Hypothesis strategy (st.integers(max_value=-1) is fc.integer({ max: -1 }))."
            ]
        else:
            snippet = hypothesis_stub(view)
            framework = "hypothesis"
            notes = []
        notes += [
            "The skeleton calls the function with each missing class but asserts nothing specific: write the "
            "property that must hold (a round trip, a conservation law, an exception type), not a snapshot "
            "of today's output.",
            "A missing class is not a bug report. Run the new test before claiming the function mishandles "
            "that input.",
        ]
        return {
            **self._head(ctx),
            "function": _fn(view.region),
            "func_key": view.region.func_key,
            "language": ctx.language,
            "framework": framework,
            "observed": sorted(view.cov.observed & {c.name for c in view.cov.expected}),
            "missing": missing,
            "snippet": snippet,
            "notes": notes,
            "caveats": [SAMPLED, view.cov.missing_reason],
        }

    # -- set cover -------------------------------------------------------------------------------------

    async def tool_minimal_test_set(
        self,
        run: str | None,
        repo: str | None,
        scope: str,
        path: str | None,
        base: str | None,
        facts: list[str] | None,
        budget_s: float | None,
        keep_property_tests: bool,
        deselect: str,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """``minimal_test_set``."""
        if scope == "path" and not path:
            raise ToolError("invalid_argument", "scope 'path' needs `path`")
        ctx = await self._ctx(run, repo)
        kinds = frozenset(facts or cover.FACT_KINDS)
        changes = None
        if scope == "changed":
            local = ctx.info.kind == "local"
            if local and not base:
                raise ToolError(
                    "invalid_argument", "scope 'changed' on a local .coverset needs `base` (a git ref)"
                )
            changes = await asyncio.to_thread(ctx.changes, base if local else None)
            if changes is None:
                raise ToolError("invalid_argument", f"run {ctx.info.run_id} has no change: pick a PR run")
        return await asyncio.to_thread(
            self._minimal,
            ctx,
            scope,
            path,
            kinds,
            changes,
            budget_s,
            keep_property_tests,
            deselect,
            limit,
            cursor,
        )

    def _minimal(
        self,
        ctx: RunContext,
        scope_name: str,
        path: str | None,
        kinds: frozenset[Any],
        changes: Any,
        budget_s: float | None,
        keep_property_tests: bool,
        deselect_mode: str,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        index = cover.cover_index(ctx)
        if scope_name == "changed":
            lines: dict[str, set[int]] = defaultdict(set)
            funcs: set[str] = set()
            for p, found in changes.changed_lines.items():
                lines[p] |= set(found)
            for change in changes.changes:
                region = change.region
                if region is not None and region.kind in ("function", "method"):
                    lines[region.path].update(range(region.body_start, region.end_line + 1))
                    funcs.add(region.func_key)
            scope = cover.Scope(kinds, None, {p: frozenset(v) for p, v in lines.items()}, frozenset(funcs))
        else:
            scope = cover.Scope(kinds, path if scope_name == "path" else None)
        selected = [i for i in range(len(index)) if scope.selects(index, i)]
        weights = {
            i: (ctx.duration(n) if ctx.duration(n) is not None else ctx.median_duration)
            for i, n in enumerate(index.nodeids)
        }
        # Tests that did not pass are kept whatever the cover says (dropping one would hide a
        # failure), and so are property-based tests unless the caller says otherwise.
        holders: set[int] = {t for i in selected for t in index.tests[i]}
        properties = property_tests(ctx)
        failing = {
            t
            for t in holders
            if (info := ctx.tests.get(index.nodeids[t])) and info.outcome not in (None, "passed")
        }
        prop_kept = {t for t in holders if index.nodeids[t] in properties} if keep_property_tests else set()
        forced = sorted(failing | prop_kept, key=lambda t: index.nodeids[t])
        forced_set = set(forced)
        rest = [i for i in selected if not forced_set.intersection(index.tests[i])]
        budget = None if budget_s is None else max(0.0, budget_s - sum(weights[t] for t in forced))
        result = cover.solve(index, rest, weights, budget_s=budget)
        kept = forced + [t for t in result.kept if t not in forced_set]
        kept_set = set(kept)
        dropped = sorted(holders - kept_set, key=lambda t: (-weights[t], index.nodeids[t]))
        grouped = result.blocks
        lost_blocks = result.uncovered_blocks
        outside = cover.singletons(index, kinds) if scope_name != "all" else Counter()

        def test_row(t: int) -> dict[str, Any]:
            nodeid = index.nodeids[t]
            info = ctx.tests.get(nodeid)
            return {
                "nodeid": nodeid,
                "duration_s": ctx.duration(nodeid),
                "outcome": info.outcome if info else None,
            }

        page, paging = _page(dropped, limit, cursor)
        everything = cover.blocks(index, selected) if forced else grouped  # forced tests' facts included
        dropped_rows = []
        for t in page:
            row = test_row(t)
            row["property_based"] = index.nodeids[t] in properties
            mine = grouped.of_test.get(t, set())
            lost = [b for b in mine if b in lost_blocks]
            lost_facts = [i for b in lost for i in grouped.members[b]]
            share: Counter[int] = Counter()
            for b in everything.of_test.get(t, ()):
                for other in everything.tests[b]:
                    if other in kept_set:
                        share[other] += 1
            row["loses"] = cover.kind_counts(index, lost_facts)
            row["loses_examples"] = [index.label(i) for i in lost_facts[:5]]
            row["covered_by"] = [
                index.nodeids[o]
                for o, _ in sorted(share.items(), key=lambda kv: (-kv[1], index.nodeids[kv[0]]))[:3]
            ]
            if scope_name != "all":
                row["unique_facts_elsewhere"] = outside.get(t, 0)
            dropped_rows.append(row)
        position = {n: i for i, n in enumerate(index.nodeids)}
        no_facts = (
            sorted(n for n in ctx.tests if position.get(n) not in holders) if scope_name == "all" else []
        )
        kept_rows = [test_row(t) for t in kept[:MAX_ROWS]]
        deselect = [index.nodeids[t] for t in dropped if not (scope_name != "all" and outside.get(t, 0))]
        if deselect_mode == "all":
            listed = deselect[:MAX_DESELECT]
        elif deselect_mode == "page":
            on_page = {index.nodeids[t] for t in page}
            listed = [n for n in deselect if n in on_page]
        else:
            listed = []
        kept_s = sum(weights[t] for t in kept)
        dropped_s = sum(weights[t] for t in dropped)
        caveats = [CANDIDATES_NOT_VERDICTS]
        if "arcs" in kinds:
            caveats.append(ARC_CAVEAT)
        if "classes" in kinds:
            caveats.append(SAMPLED)
        if failing:
            caveats.append(f"{len(failing)} test(s) that did not pass are always kept.")
        if prop_kept:
            caveats.append(
                f"{len(prop_kept)} property-based test(s) are always kept (keep_property_tests=false to "
                "let the cover drop them)."
            )
        if scope_name != "all":
            caveats.append(
                "The scope is part of the run: a dropped test may still be the only one reaching facts "
                "outside it (`unique_facts_elsewhere`); those are left out of deselect_args."
            )
        return {
            **self._head(ctx),
            "scope": scope_name,
            "path": path,
            "facts": sorted(kinds),
            "universe": {
                **cover.kind_counts(index, selected),
                "blocks": len({index.tests[i] for i in selected}),
                "tests_in_scope": len(holders),
                "arcs_unattributed": index.arcs_unattributed if "arcs" in kinds else 0,
            },
            "exhaustive": result.exhaustive,
            "kept": {
                "count": len(kept),
                "duration_s": round(kept_s, 3),
                "tests": kept_rows,
                "truncated": len(kept) > MAX_ROWS,
            },
            "dropped": {
                "count": len(dropped),
                "duration_s": round(dropped_s, 3),
                "saving_pct": _pct(round(dropped_s * 1000), round((kept_s + dropped_s) * 1000)),
                "tests": dropped_rows,
                **paging,
            },
            "no_facts": no_facts[:MAX_ROWS],
            "deselect": deselect_mode,
            "deselect_args": "".join(f"--deselect={n}\n" for n in listed),
            "deselect_count": len(deselect),
            "deselect_truncated": len(listed) < len(deselect),
            "caveats": caveats,
        }

    # -- redundancy and the test tax --------------------------------------------------------------------

    async def tool_redundant_tests(
        self, run: str | None, repo: str | None, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        """``redundant_tests``: the test economy's equivalence groups, dominance and removable
        candidates (:func:`coverset.report.economy.redundancy_of`)."""
        ctx = await self._ctx(run, repo)
        view, why = await asyncio.to_thread(economy_for, ctx)
        if view is None:
            return {
                **self._head(ctx),
                "available": False,
                "reason": why,
                "equivalent_groups": [],
                "dominated": [],
            }
        found = redundancy_of(view)
        rows = view.by_nodeid()

        def seconds(nodeid: str) -> float:
            row = rows.get(nodeid)
            return (row.duration_ms or 0.0) / 1000.0 if row is not None else 0.0

        groups = [
            {
                "tests": list(g.nodeids[:50]),
                "size": len(g.nodeids),
                "keep": g.keep,
                "seconds_if_one_kept": round(sum(seconds(n) for n in g.nodeids if n != g.keep), 4),
            }
            for g in found.groups
        ]
        dominated = [
            {
                "test": test,
                "dominated_by": by,
                "duration_s": seconds(test) or None,
                "unique_facts": rows[test].unique_facts if test in rows else None,
            }
            for test, by in found.dominated
        ]
        dominated.sort(key=lambda d: (-(d["duration_s"] or 0.0), d["test"]))
        group_page, group_paging = _page(groups, limit, cursor)
        dom_page, dom_paging = _page(dominated, limit, cursor)
        rem_page, rem_paging = _page(found.removable, limit, cursor)
        return {
            **self._head(ctx),
            "available": True,
            "source": f"test economy: {_economy_source(view)}",
            "equivalent_groups": group_page,
            "equivalent_groups_total": group_paging["total"],
            "dominated": dom_page,
            "dominated_total": dom_paging["total"],
            "removable_candidates": [
                {
                    "nodeid": r.nodeid,
                    "duration_ms": r.duration_ms,
                    "statements": r.statements,
                    "asserts": r.asserts,
                    "dominated_by": r.dominated_by,
                }
                for r in rem_page
            ],
            "removable_total": rem_paging["total"],
            "collapsible_families": [
                {
                    "family": f.family,
                    "path": f.path,
                    "cases": f.cases,
                    "distinct_flows": f.distinct_flows,
                    "redundant_cases": f.redundant_cases,
                }
                for f in view.families
                if f.redundant_cases
            ][: min(limit, MAX_ROWS)],
            "minimal_set_size": found.minimal_set_size,
            "tests": found.tests,
            "next_cursor": group_paging["next_cursor"]
            or dom_paging["next_cursor"]
            or rem_paging["next_cursor"],
            "notes": list(view.notes),
            "caveats": [
                CANDIDATES_NOT_VERDICTS,
                found.caveat,
                "Facts compared: lines, inferred branch arcs and shape observations per test; assertions, "
                "mocks and expected exceptions are not facts coverset records.",
                "minimal_set_size is the economy's cover over every distinct argument shape, finer than the "
                "input classes minimal_test_set keeps, so minimal_test_set's set is usually smaller.",
            ],
        }

    async def tool_test_tax(
        self, run: str | None, repo: str | None, sort: str, only: str, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        """``test_tax``."""
        ctx = await self._ctx(run, repo)
        view, why = await asyncio.to_thread(economy_for, ctx)
        if view is None:
            return {**self._head(ctx), "available": False, "reason": why, "pr_tax": None, "tests": []}
        rows = [
            {**(_fields(r) or {}), "not_measured_reason": None if r.measured else r.reason}
            for r in view.tests
        ]
        if only == "assertion_free":
            rows = [r for r in rows if r["assertion_free"]]
        elif only == "removable":
            rows = [r for r in rows if r["removable"]]
        elif only == "unmeasured":
            rows = [r for r in rows if not r["measured"]]
        column, descending = _TAX_SORTS[sort]
        present = sorted((r for r in rows if r[column] is not None), key=lambda r: r["nodeid"])
        if sort == "leverage":  # among equal leverage, the longest test first
            present.sort(key=lambda r: -((r["statements"] or 0) + (r["helper_statements"] or 0)))
        present.sort(key=lambda r: r[column], reverse=descending)
        rows = present + sorted((r for r in rows if r[column] is None), key=lambda r: r["nodeid"])
        page, paging = _page(rows, limit, cursor)
        fields = (
            "nodeid", "statements", "helper_statements", "asserts", "fixtures", "cases", "unique_facts",
            "unique_lines", "unique_arcs", "unique_classes", "leverage", "duration_ms", "cost_per_fact_ms",
            "assertion_free", "removable", "in_minimal_set", "measured", "not_measured_reason",
        )  # fmt: skip
        return {
            **self._head(ctx),
            "available": True,
            "source": _economy_source(view),
            "weights": dict(view.weights),
            "totals": _fields(view.totals),
            "pr_tax": _fields(view.tax),
            "sort": sort,
            "only": only,
            "tests": [{k: r[k] for k in fields} for r in page],
            **paging,
            "notes": list(view.notes),
            "caveats": [
                "Leverage is unique facts (lines + arcs + 3 x input classes no other test reaches) per test "
                "statement (own plus helpers): a long test with low leverage costs review time for little.",
                CANDIDATES_NOT_VERDICTS,
            ],
        }

    # -- search and grammar --------------------------------------------------------------------------

    async def tool_search(
        self, run: str | None, repo: str | None, query: str, kinds: list[str] | None, limit: int
    ) -> dict[str, Any]:
        """``search``."""
        ctx = await self._ctx(run, repo)
        return await asyncio.to_thread(self._search, ctx, query, kinds, limit)

    def _search(self, ctx: RunContext, query: str, kinds: list[str] | None, limit: int) -> dict[str, Any]:
        """``search``'s body, run in a worker thread: it may load the functions and test tables."""
        wanted = set(kinds or ("functions", "files", "tests"))
        folded = query.casefold()
        cap = min(limit, MAX_ROWS)
        out: dict[str, Any] = {**self._head(ctx), "query": query}
        if "functions" in wanted:
            found = [r for r in ctx.callables if folded in _fn(r).casefold()]
            found.sort(key=lambda r: (folded not in r.qualname.casefold(), len(r.qualname), _fn(r)))
            out["functions"] = [
                {"function": _fn(r), "func_key": r.func_key, "def_line": r.def_line} for r in found[:cap]
            ]
            out["functions_total"] = len(found)
        if "files" in wanted:
            paths = sorted(p for p in ctx.facts.statics if folded in p.casefold())
            out["files"] = paths[:cap]
            out["files_total"] = len(paths)
        if "tests" in wanted:
            names = sorted(n for n in set(ctx.tests) | set(ctx.test_ids) if folded in n.casefold())
            out["tests"] = names[:cap]
            out["tests_total"] = len(names)
        return out

    async def tool_explain_shape_key(self, key: str) -> dict[str, Any]:
        """``explain_shape_key``."""
        return explain_shape_key(key)


def _path_suffix(path: str, spec: str) -> bool:
    """Whether ``spec`` names ``path`` by a whole-component suffix (``utils.py`` but not ``ils.py``)."""
    spec = spec.removeprefix("./")
    return path == spec or path.endswith("/" + spec)


def _resolve_path(paths: Mapping[str, Any], spec: str) -> str:
    """The one measured file ``spec`` names, exactly or by a path-component suffix.

    Two files that share the suffix (``pkg_a/utils.py`` and ``pkg_b/utils.py``) are an
    ``ambiguous`` error listing both, never a silent pick: the agent would otherwise read the
    wrong function's coverage with no sign anything went astray.
    """
    if spec in paths:
        return spec
    matches = sorted(p for p in paths if _path_suffix(p, spec))
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ToolError(
            "ambiguous",
            f"{spec!r} matches {len(matches)} files: pass a longer path",
            {"candidates": matches[:20]},
        )
    near = sorted(p for p in paths if spec.casefold() in p.casefold())[:20]
    raise ToolError("not_found", f"{spec} is not a measured file of this run", {"similar": near})


def _fast_check_snippet(view: FunctionView, missing: list[dict[str, Any]]) -> str | None:
    """A Vitest + fast-check skeleton for the first missing class of each parameter."""
    first: dict[str, dict[str, Any]] = {}
    for row in missing:
        first.setdefault(row["param"], row)
    if not first:
        return None
    name = view.region.qualname.split(".")[-1]
    arbitraries = ", ".join(
        row["fast_check"] or f"/* translate: {row['hypothesis']} */ fc.anything()" for row in first.values()
    )
    params = ", ".join(first)
    call_args = (
        ", ".join(
            p if p in first else "/* value */ undefined" for p in (q.lstrip("*") for q in view.region.params)
        )
        or params
    )
    return (
        'import { test } from "vitest";\n'
        'import fc from "fast-check";\n'
        f'import {{ {name} }} from "./{view.region.path.rsplit("/", 1)[-1].rsplit(".", 1)[0]}";\n\n'
        f'test("{name} handles {", ".join(r["class"] for r in first.values())}", () => {{\n'
        f"  fc.assert(\n"
        f"    fc.property({arbitraries}, ({params}) => {{\n"
        f"      const result = {name}({call_args});\n"
        f"      // assert what must hold for these inputs, e.g. an invariant of the result\n"
        f"    }}),\n"
        f"  );\n"
        f"}});\n"
    )
