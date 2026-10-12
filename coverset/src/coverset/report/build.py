"""Build the static report: every page from one :class:`~coverset.report.facts.ReportInput`.

Pages (all relative links, so the directory works from ``file://`` and from any static host):

``index.html``
    totals, the treemap (Gap and Lines), the file table, functions with never-exercised input
    classes, every test.
``files/<path>.html``
    the source through :func:`~coverset.web.render.source.render_source` (the pages' renderer),
    covering-test counts that open the line's tests, inlay chips, shape grids (key ``i``) and the
    test lens (dim what the selected test did not run). Unsafe paths get hashed names.
``functions/<func_key>.html``
    signature, body with coverage, shape grid, flow ribbon, covering tests, input classes with
    examples or Hypothesis strategies, the sampling note.
``tests/<hash>.html``
    one test: outcome, duration, message, the files and functions it ran, the input classes it
    passed in.
``about.html``
    producer, tracer, caps and limits hit, the bundle's meta.

Every value from the bundle is untrusted: it reaches markup only through Jinja autoescaping or the
``coverset.web.render`` helpers (inside :func:`~coverset.web.render.html.relative_hrefs`, so the
renderers' links may be relative), data blocks go through
:func:`~coverset.web.render.html.json_data`, and every written file passes
:func:`~coverset.report.output.neutralize` (bidirectional controls made visible).
"""

from __future__ import annotations

import importlib.resources
import json
import os
import re
import shutil
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import jinja2
from markupsafe import Markup

from coverset.analysis.functions import MODULE_QUALNAME
from coverset.analysis.shapes import NEVER_EXERCISED
from coverset.facts.queries import TreeNode
from coverset.facts.schema import CALLABLE_KINDS
from coverset.lang.base import IMPORT_CONTEXT, FunctionRegion
from coverset.report.facts import NO_RESULT, NOT_RUN, ReportInput, TestInfo
from coverset.report.output import (
    PageNames,
    Writer,
    check_out_dir,
    hashed_name,
    neutralize,
    neutralize_js,
    staging_dir,
    swap_in,
)
from coverset.units import fmt_ms
from coverset.web.facts_view import FunctionView, test_file
from coverset.web.render.html import breakable, json_data, relative_hrefs
from coverset.web.render.ribbon import render_ribbon
from coverset.web.render.shapegrid import GridCell, render_grid, render_inlay, render_strip
from coverset.web.render.source import LineFacts, RegionMark, render_source
from coverset.web.render.treemap import build_tree, render_legend, render_treemap

__all__ = ["BuildResult", "build_report"]

GRID_W_FILE = 860.0
GRID_W_FN = 580.0
FILE_LIST_WINDOW = 200
"""Beyond twice this many files, a file page lists only this many neighbours on each side."""
MAX_META_CHARS = 200_000
"""The about page's verbatim meta dump is cut here (and says so)."""
MAX_MESSAGE_CHARS = 8 * 1024
MAX_LISTED = 200
"""A test page lists at most this many functions it ran, a function page at most this many covering
tests (then a "+N more" line), so the report grows with tests + functions, not their product."""
_FUNC_KEY = re.compile(r"^[0-9a-f]{16}$")
OUTCOME_ORDER = ("failed", "error", "xpassed", "passed", "xfailed", "skipped", NO_RESULT, NOT_RUN)
_OUTCOME_RANK = {name: i for i, name in enumerate(OUTCOME_ORDER)}


def outcome_rank(outcome: str | None) -> int:
    """Sort rank of an outcome label (an unknown label sorts after every known one)."""
    return _OUTCOME_RANK.get(outcome or NOT_RUN, len(OUTCOME_ORDER))


@dataclass(frozen=True, slots=True)
class BuildResult:
    """What a build wrote."""

    out_dir: Path
    index: Path
    pages: dict[str, int]
    """Page counts by kind (index, about, files, functions, tests)."""


@dataclass(slots=True)
class FnEntry:
    """One callable region with its view and pages."""

    region: FunctionRegion
    view: FunctionView
    page: str
    file_page: str
    tests: list[str]
    hollow: int

    @property
    def called(self) -> bool:
        return bool(self.tests)

    @property
    def never_exercised(self) -> bool:
        return self.hollow > 0 and self.view.cov.missing_reason == NEVER_EXERCISED


@dataclass(slots=True)
class FileEntry:
    """One measured file."""

    path: str
    page: str
    source: str | None
    facts: LineFacts
    functions: list[FnEntry] = field(default_factory=list)
    classes: list[FunctionRegion] = field(default_factory=list)
    tests: dict[str, frozenset[int]] = field(default_factory=dict)
    """Tests (not ``<import>``) that ran lines of this file, with those lines."""
    import_lines: frozenset[int] = frozenset()

    @property
    def statements(self) -> int:
        return len(self.facts.statements - self.facts.excluded)

    @property
    def run(self) -> int:
        return sum(1 for n in self.facts.statements - self.facts.excluded if n in self.facts.executed)

    @property
    def missed(self) -> int:
        return sum(1 for n in self.facts.statements if self.facts.state(n) == "mis")

    @property
    def partial(self) -> int:
        return len(self.facts.partial)

    @property
    def run_pct(self) -> float | None:
        return 100.0 * self.run / self.statements if self.statements else None

    @property
    def expected(self) -> int:
        return sum(len(f.view.cov.expected) for f in self.functions if f.view.observations)

    @property
    def observed(self) -> int:
        return sum(f.view.cov.observed_expected for f in self.functions if f.view.observations)

    @property
    def hollow(self) -> int:
        return sum(f.hollow for f in self.functions)

    @property
    def gap(self) -> float | None:
        """Line coverage % minus input-class coverage % (None when no class is expected)."""
        if not self.expected or self.run_pct is None:
            return None
        return max(0.0, self.run_pct - 100.0 * self.observed / self.expected)


@dataclass(slots=True)
class TestEntry:
    """One test with its page."""

    __test__ = False

    info: TestInfo
    hash: str
    page: str
    files: dict[str, frozenset[int]]


# -- environment ----------------------------------------------------------------------------------


def _pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.0f}%" if abs(value - round(value)) < 0.05 else f"{value:.1f}%"


def _plural(count: int, singular: str, plural_form: str | None = None) -> str:
    return f"{count} {singular if count == 1 else (plural_form or singular + 's')}"


def _duration(seconds: float | None) -> str:
    return fmt_ms(seconds * 1000.0 if seconds is not None else None)


def environment() -> jinja2.Environment:
    """The report's Jinja environment (autoescaping on, strict undefined)."""
    env = jinja2.Environment(
        loader=jinja2.PackageLoader("coverset.report", "templates"),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["nodeid"] = breakable
    env.filters["pct"] = _pct
    env.filters["plural"] = _plural
    env.filters["duration"] = _duration
    return env


def _asset(name: str) -> str:
    return (importlib.resources.files("coverset.report") / "static" / name).read_text(encoding="utf-8")


def stylesheet() -> str:
    """``report.css``: the pages' ``app.css`` (same tokens, colours and components) plus the
    report's own rules."""
    app = (importlib.resources.files("coverset.web") / "static" / "app.css").read_text(encoding="utf-8")
    return app.rstrip("\n") + "\n\n" + _asset("report.css")


# -- the build ------------------------------------------------------------------------------------


def build_report(inp: ReportInput, out: Path) -> BuildResult:
    """Write the report for ``inp`` into ``out`` (see :func:`~coverset.report.output.check_out_dir`).

    The pages are written into a staging directory next to ``out`` that replaces it only when the
    build succeeded: a failed build leaves the previous report untouched.
    """
    target = check_out_dir(out)
    staging = staging_dir(target)
    try:
        writer = Writer(staging)
        with relative_hrefs():
            builder = _Builder(inp, writer)
            builder.collect()
            pages = builder.write_all()
        root = swap_in(staging, target)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return BuildResult(out_dir=root, index=root / "index.html", pages=pages)


def _rel(depth_of: str, target: str) -> str:
    """``target`` (relative to the report root) as seen from the page ``depth_of``."""
    depth = depth_of.count("/")
    return "../" * depth + target


def _func_page(region: FunctionRegion) -> str:
    key = region.func_key if _FUNC_KEY.match(region.func_key) else hashed_name(region.func_key)
    return f"functions/{key}.html"


class _Builder:
    def __init__(self, inp: ReportInput, writer: Writer) -> None:
        self.inp = inp
        self.facts = inp.facts
        self.writer = writer
        self.env = environment()
        self.names = PageNames()
        self.files: dict[str, FileEntry] = {}
        self.functions: list[FnEntry] = []
        self.tests: dict[str, TestEntry] = {}

    # -- collection -------------------------------------------------------------------------

    def collect(self) -> None:
        facts = self.facts
        paths = sorted(facts.statics)
        for path in paths:
            self.files[path] = FileEntry(
                path=path,
                page=self.names.file_page(path),
                source=self.inp.sources.get(path),
                facts=facts.line_facts(path),
            )
        for info in self.inp.tests:
            digest = hashed_name(info.nodeid)
            self.tests[info.nodeid] = TestEntry(info, digest, f"tests/{digest}.html", {})
        for nodeid, by_path in sorted(self.inp.coverage.items()):
            if nodeid == IMPORT_CONTEXT:
                for path, lines in by_path.items():
                    if path in self.files:
                        self.files[path].import_lines = frozenset(lines)
                continue
            entry = self.tests.get(nodeid)
            if entry is None:
                digest = hashed_name(nodeid)
                info = TestInfo(nodeid, nodeid.split("::", 1)[0], NO_RESULT)
                entry = self.tests[nodeid] = TestEntry(info, digest, f"tests/{digest}.html", {})
            for path, lines in sorted(by_path.items()):
                if path in self.files and lines:
                    entry.files[path] = frozenset(lines)
                    self.files[path].tests[nodeid] = frozenset(lines)
        for region in sorted(facts.regions, key=lambda r: (r.path, r.def_line, r.qualname)):
            entry = self.files.get(region.path)
            if entry is None:
                continue
            if region.kind == "class":
                entry.classes.append(region)
                continue
            if region.kind not in CALLABLE_KINDS:
                continue
            view = facts.function_view(region, entry.source)
            fn = FnEntry(
                region=region,
                view=view,
                page=_func_page(region),
                file_page=entry.page,
                tests=facts.tests_in_range(region.path, region.body_start, region.end_line),
                hollow=view.hollow if view.observations else 0,
            )
            entry.functions.append(fn)
            self.functions.append(fn)

    # -- shared bits -------------------------------------------------------------------------

    def render(self, page: str, template: str, **context: Any) -> None:
        root = _rel(page, "")
        html = self.env.get_template(template).render(
            root=root,
            page=page,
            title=self.inp.title,
            origin=self.inp.origin,
            notice=self.inp.notice,
            link=lambda target: _rel(page, target),
            **context,
        )
        self.writer.text(page, neutralize(html))

    def test_link(self, nodeid: str) -> str | None:
        entry = self.tests.get(nodeid)
        return entry.page if entry else None

    def outcome(self, nodeid: str) -> str | None:
        entry = self.tests.get(nodeid)
        return entry.info.outcome if entry else None

    @staticmethod
    def data_block(value: object) -> Markup:
        return Markup(neutralize_js(str(json_data(value))))

    # -- pages -------------------------------------------------------------------------------

    def write_all(self) -> dict[str, int]:
        self.writer.text("report.css", stylesheet())
        self.writer.text("report.js", _asset("report.js"))
        self.writer.text("search-index.js", self.search_index())
        self.index_page()
        self.about_page()
        ordered = list(self.files)
        for i, path in enumerate(ordered):
            prev_page = self.files[ordered[i - 1]].page if i > 0 else None
            next_page = self.files[ordered[i + 1]].page if i + 1 < len(ordered) else None
            self.file_page(self.files[path], prev_page, next_page)
        for fn in self.functions:
            self.function_page(fn)
        for entry in self.tests.values():
            self.test_page(entry)
        return {
            "index": 1,
            "about": 1,
            "files": len(self.files),
            "functions": len(self.functions),
            "tests": len(self.tests),
        }

    def search_index(self) -> str:
        items: list[list[str]] = []
        for path, entry in self.files.items():
            items.append(["file", path, "", entry.page])
        for fn in self.functions:
            items.append(["function", fn.region.qualname, fn.region.path, fn.page])
        for nodeid, test in sorted(self.tests.items()):
            items.append(["test", nodeid, test.info.outcome or NOT_RUN, test.page])
        payload = neutralize_js(str(json_data(items)))
        return f"/* coverset report search index (generated) */\nwindow.COVERSET_SEARCH = {payload};\n"

    # -- index -------------------------------------------------------------------------------

    def totals(self) -> dict[str, Any]:
        files = list(self.files.values())
        statements = sum(f.statements for f in files)
        run = sum(f.run for f in files)
        branches = 0
        taken = 0
        for path in self.files:
            static = self.facts.statics[path]
            arcs = self.facts.run_arcs.get(path, frozenset())
            branches += len(static.arcs_possible)
            taken += sum(1 for arc in static.arcs_possible if arc in arcs)
        outcomes = Counter(t.info.outcome or NOT_RUN for t in self.tests.values())
        capped = sum(1 for f in self.functions if f.hollow and not f.never_exercised)
        return {
            "statements": statements,
            "run": run,
            "run_pct": 100.0 * run / statements if statements else None,
            "missed": sum(f.missed for f in files),
            "branches": branches,
            "branches_taken": taken,
            "branch_pct": 100.0 * taken / branches if branches else None,
            "partial": sum(f.partial for f in files),
            "files": len(files),
            "functions": len(self.functions),
            "called": sum(1 for f in self.functions if f.called),
            "never_exercised": sum(1 for f in self.functions if f.never_exercised),
            "capped": capped,
            "hollow_classes": sum(f.hollow for f in self.functions),
            "tests": len(self.tests),
            "outcomes": sorted(outcomes.items(), key=lambda kv: (outcome_rank(kv[0]), kv[0])),
        }

    def tree_nodes(self) -> list[TreeNode]:
        """Treemap nodes as :func:`coverset.facts.queries.treemap` builds them (statements belong
        to the innermost function body holding them, else to ``<module>``), with input-class
        coverage from the report's own function views."""
        nodes: list[TreeNode] = []
        files_acc: dict[str, list[int]] = {}
        dirs_acc: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        leaves: dict[str, list[TreeNode]] = defaultdict(list)
        for path, entry in self.files.items():
            statements = sorted(entry.facts.statements - entry.facts.excluded)
            owner: dict[int, FnEntry | None] = {}
            for line in statements:
                best: FnEntry | None = None
                for fn in entry.functions:
                    r = fn.region
                    if r.body_start <= line <= r.end_line and (
                        best is None or r.body_start > best.region.body_start
                    ):
                        best = fn
                owner[line] = best
            per: dict[str | None, list[int]] = defaultdict(lambda: [0, 0])
            for line, fn in owner.items():
                key = fn.region.func_key if fn else None
                per[key][0] += 1
                per[key][1] += 1 if line in entry.facts.executed else 0
            acc = [0, 0, 0, 0]
            module = per.get(None)
            if module and module[0]:
                leaves[path].append(
                    TreeNode(f"{path}::{MODULE_QUALNAME}", "function", module[0], module[1], None)
                )
            for fn in entry.functions:
                stm, cov = per.get(fn.region.func_key, [0, 0])
                expected = len(fn.view.cov.expected) if fn.view.observations else 0
                observed = fn.view.cov.observed_expected if fn.view.observations else 0
                input_cov = observed / expected if expected else None
                leaves[path].append(
                    TreeNode(
                        f"{path}::{fn.region.qualname}",
                        "function",
                        stm,
                        cov,
                        input_cov,
                        func_key=fn.region.func_key,
                        def_line=fn.region.def_line,
                    )
                )
                acc[2] += expected
                acc[3] += observed
            acc[0] = sum(v[0] for v in per.values())
            acc[1] = sum(v[1] for v in per.values())
            files_acc[path] = acc
            parts = path.split("/")
            for depth in range(1, len(parts)):
                d = dirs_acc["/".join(parts[:depth])]
                for i in range(4):
                    d[i] += acc[i]
        emitted: set[str] = set()
        for path in sorted(files_acc):
            parts = path.split("/")
            for depth in range(1, len(parts)):
                prefix = "/".join(parts[:depth])
                if prefix not in emitted:
                    emitted.add(prefix)
                    d = dirs_acc[prefix]
                    nodes.append(TreeNode(prefix, "dir", d[0], d[1], d[3] / d[2] if d[2] else None))
            a = files_acc[path]
            nodes.append(TreeNode(path, "file", a[0], a[1], a[3] / a[2] if a[2] else None))
            nodes.extend(leaves[path])
        return nodes

    def index_page(self) -> None:
        page = "index.html"
        tree = build_tree(self.tree_nodes())
        file_pages = {path: e.page for path, e in self.files.items()}

        def leaf_url(item: Any) -> str:
            target = file_pages.get(item.file or "")
            if target is None:
                return "#"
            return f"{target}#L{item.def_line}" if item.def_line else target

        def dir_url(item: Any) -> str:
            target = file_pages.get(item.path)
            return target or "#files"

        maps = {
            mode: render_treemap(
                tree,
                mode=mode,
                width=1240.0,
                height=520.0,
                leaf_url=leaf_url,
                dir_url=dir_url,
                label=f"Treemap of statements by function ({'gap' if mode == 'gap' else 'line coverage'})",
            )
            for mode in ("gap", "lines")
        }
        legends = {mode: render_legend(mode) for mode in ("gap", "lines")}
        gaps = sorted(
            (f for f in self.functions if f.hollow),
            key=lambda f: (-f.hollow, f.region.path, f.region.def_line),
        )
        tests = sorted(
            self.tests.values(),
            key=lambda t: (outcome_rank(t.info.outcome), t.info.nodeid),
        )
        self.render(
            page,
            "index.html",
            kind="index",
            totals=self.totals(),
            files=list(self.files.values()),
            maps=maps,
            legends=legends,
            gaps=gaps,
            tests=tests,
            strip=lambda fn: render_strip(fn.view.cells(), missing=fn.view.cov.missing_reason),
            data=None,
        )

    def about_page(self) -> None:
        meta = self.inp.meta
        inner = meta.get("meta") if isinstance(meta.get("meta"), dict) else {}
        flat = {**(inner.get("meta") if isinstance(inner.get("meta"), dict) else {}), **inner}
        convert = flat.get("convert") if isinstance(flat.get("convert"), dict) else {}
        producer = flat.get("producer") if isinstance(flat.get("producer"), dict) else None
        rows: list[tuple[str, str]] = [
            ("Source", str(meta.get("source", ""))),
            ("Read as", str(meta.get("bundle_kind", ""))),
        ]
        rows.append(("Sources read from", str(meta.get("repo_root", ""))))
        if meta.get("sources"):
            rows.append(("Sources", str(meta["sources"])))
        if producer:
            rows.append(("Producer", f"{producer.get('name', '?')} {producer.get('version', '')}".strip()))
        elif flat.get("plugin_version"):
            rows.append(("Producer", f"pytest-coverset {flat.get('plugin_version')}"))
        if flat.get("language"):
            rows.append(("Language", str(flat["language"])))
        python = flat.get("python")
        if isinstance(python, dict):
            rows.append(("Python", f"{python.get('implementation', '')} {python.get('version', '')}".strip()))
        elif python:
            rows.append(("Python", str(python)))
        for key, label in (
            ("coverage_version", "coverage.py"),
            ("pytest_version", "pytest"),
            ("tracer", "Tracer"),
            ("core", "Tracer core"),
            ("shapes", "Shape tracer"),
            ("repr_examples", "Examples recorded"),
            ("shape_samples", "Sample cap (calls per function)"),
            ("shape_settle", "Settle window"),
            ("full_trace_cap", "Full-trace safety cap"),
            ("functions_traced", "Functions traced"),
            ("functions_truncated", "Functions that hit the sample cap"),
            ("tracer_errors", "Tracer errors"),
            ("tracer_lost_at", "Tracer lost at"),
        ):
            if key in flat and flat[key] not in (None, "", []):
                rows.append((label, _short(flat[key])))
        limits: list[tuple[str, str]] = []
        for key, value in sorted((convert.get("dropped") or {}).items()):
            limits.append((str(key), str(value)))
        if convert.get("tracer_mismatch"):
            limits.append(("tracer_mismatch", str(convert.get("tracer"))))
        for warning in flat.get("coverage_warnings") or []:
            limits.append(("coverage warning", _short(warning)))
        for error in flat.get("errors") or []:
            limits.append(("plugin error", _short(error)))
        for shard in meta.get("ingest") or []:
            dropped = ((shard.get("convert") or {}).get("dropped") or {}) if isinstance(shard, dict) else {}
            for key, value in sorted(dropped.items()):
                limits.append((f"shard {shard.get('shard')}: {key}", str(value)))
        capped = [f for f in self.functions if f.view.cov.tests_truncated]
        dump = json.dumps(meta, indent=2, sort_keys=True, default=str, ensure_ascii=False)
        if len(dump) > MAX_META_CHARS:
            dump = (
                dump[:MAX_META_CHARS]
                + f"\n… (cut: the meta is {len(dump):,} characters, this page shows the first {MAX_META_CHARS:,})"
            )
        run_rows = [
            (k, _short(v))
            for k, v in (meta.get("run") or {}).items()
            if v not in (None, "", {}) and k != "summary"
        ]
        self.render(
            "about.html",
            "about.html",
            kind="about",
            rows=rows,
            run_rows=run_rows,
            limits=limits,
            capped=capped,
            dump=dump,
            data=None,
        )

    # -- file pages ---------------------------------------------------------------------------

    def file_page(self, entry: FileEntry, prev_page: str | None, next_page: str | None) -> None:
        page = entry.page
        regions: list[RegionMark] = []
        templates: list[tuple[str, Markup]] = []
        for fn in entry.functions:
            view = fn.view
            chips = None
            if view.cov.expected:
                chips = render_inlay(
                    view.chips(),
                    note=view.cov.note,
                    shapes_url=f"#grid-{fn.region.func_key[:16]}",
                    missing=view.cov.missing_reason,
                )
                templates.append((fn.region.func_key, self.grid(fn, GRID_W_FILE)))
            regions.append(
                RegionMark(
                    start=fn.region.def_line,
                    end=fn.region.end_line,
                    qualname=fn.region.qualname,
                    kind=fn.region.kind,
                    func_key=fn.region.func_key,
                    hollow=fn.hollow,
                    url=_rel(page, fn.page),
                    chips=chips,
                )
            )
        for region in entry.classes:
            regions.append(RegionMark(region.def_line, region.end_line, region.qualname, "class"))
        html = None
        if entry.source is not None:
            html = render_source(
                entry.source,
                entry.facts,
                path=entry.path,
                regions=regions,
                tests_url=lambda n: f"#L{n}",
            )
        tests = sorted(entry.tests.items())
        data = {
            "kind": "file",
            "path": entry.path,
            "tests": [
                [nodeid, self.outcome(nodeid) or NOT_RUN, self.tests[nodeid].hash, sorted(lines)]
                for nodeid, lines in tests
            ],
            "import": sorted(entry.import_lines),
        }
        missed_lines = sorted(n for n in entry.facts.statements if entry.facts.state(n) == "mis")
        self.render(
            page,
            "file.html",
            kind="file",
            entry=entry,
            html=html,
            grids=templates,
            files=self.file_window(entry.path),
            prev_page=prev_page,
            next_page=next_page,
            lens_tests=[
                (i, nodeid, self.outcome(nodeid), len(lines)) for i, (nodeid, lines) in enumerate(tests)
            ],
            missed_lines=_ranges(missed_lines),
            partial_lines=sorted(entry.facts.partial.items()),
            data=self.data_block(data),
        )

    @cached_property
    def file_labels(self) -> list[tuple[FileEntry, str]]:
        """Each file with its label in the file list (the directory prefix all files share left out)."""
        dirs = [path.split("/")[:-1] for path in self.files]
        common: list[str] = []
        if len(dirs) > 1:
            for parts in zip(*dirs, strict=False):
                if len(set(parts)) != 1:
                    break
                common.append(parts[0])
        cut = len("/".join(common)) + 1 if common else 0
        return [(e, e.path[cut:]) for e in self.files.values()]

    def file_window(self, path: str) -> tuple[list[tuple[FileEntry, str]], int, int]:
        """The file list of one file page: every file, or (in a large repository) the neighbours of
        ``path``, with how many files were left out before and after."""
        labels = self.file_labels
        if len(labels) <= 2 * FILE_LIST_WINDOW:
            return labels, 0, 0
        index = next(i for i, (e, _) in enumerate(labels) if e.path == path)
        lo = max(0, index - FILE_LIST_WINDOW)
        hi = min(len(labels), index + FILE_LIST_WINDOW + 1)
        return labels[lo:hi], lo, len(labels) - hi

    def grid(self, fn: FnEntry, width: float) -> Markup:
        model = fn.view.grid()
        grid = render_grid(model, cell_url=lambda cell: "#", target=f"sg-{fn.region.func_key}", width=width)
        table = self.env.get_template("_classes.html").render(
            fn=fn,
            cells=self.grid_cells(fn),
            compact=True,
            link=lambda target: target,
            test_link=lambda nodeid: None,
            uid=f"{fn.region.func_key}-f",
        )
        return grid + Markup(table)

    @staticmethod
    def grid_cells(fn: FnEntry) -> list[GridCell]:
        """The grid's cells in the order the SVG draws them (rows in order, cells in order)."""
        out: list[GridCell] = []
        for row in fn.view.grid().rows:
            out.extend(row.cells)
        return out

    # -- function pages -----------------------------------------------------------------------

    def function_page(self, fn: FnEntry) -> None:
        from coverset.web.detail import hypothesis_stub

        page = fn.page
        region = fn.region
        entry = self.files[region.path]
        view = fn.view
        hunk = None
        if entry.source is not None:
            hunk = render_source(
                entry.source,
                entry.facts,
                path=region.path,
                line_range=(region.def_line, region.end_line),
                tests_url=lambda n: f"#L{n}",
                anchor_prefix=f"F{region.func_key[:8]}-",
            )
        grid = render_grid(
            view.grid(), cell_url=lambda cell: "#", target=f"sg-{region.func_key}", width=GRID_W_FN
        )
        ribbon = render_ribbon(view.flows(test_file), width=GRID_W_FN)
        tests_here = {
            nodeid: frozenset(n for n in lines if region.def_line <= n <= region.end_line)
            for nodeid, lines in entry.tests.items()
        }
        data = {
            "kind": "function",
            "path": region.path,
            "tests": [
                [nodeid, self.outcome(nodeid) or NOT_RUN, self.tests[nodeid].hash, sorted(lines)]
                for nodeid, lines in sorted(tests_here.items())
                if lines
            ],
            "import": sorted(n for n in entry.import_lines if region.def_line <= n <= region.end_line),
        }
        outcomes = Counter(self.outcome(t) or NOT_RUN for t in fn.tests)
        covering = sorted(fn.tests, key=lambda n: (outcome_rank(self.outcome(n)), n))
        self.render(
            page,
            "function.html",
            kind="function",
            fn=fn,
            entry=entry,
            signature=_signature(region, view),
            hunk=hunk,
            grid=grid,
            ribbon=ribbon,
            cells=self.grid_cells(fn),
            covering=[(n, self.outcome(n), self.test_link(n)) for n in covering[:MAX_LISTED]],
            covering_more=max(0, len(covering) - MAX_LISTED),
            outcomes=sorted(outcomes.items(), key=lambda kv: (outcome_rank(kv[0]), kv[0])),
            suggestion=hypothesis_stub(view),
            test_link=self.test_link,
            data=self.data_block(data),
        )

    # -- test pages ---------------------------------------------------------------------------

    def test_page(self, entry: TestEntry) -> None:
        page = entry.page
        nodeid = entry.info.nodeid
        files = []
        for path, lines in sorted(entry.files.items()):
            file_entry = self.files[path]
            statements = file_entry.facts.statements - file_entry.facts.excluded
            files.append((file_entry, len(lines & statements), len(statements)))
        functions = []
        for file_entry, _, _ in files:
            lines = entry.files[file_entry.path]
            for fn in file_entry.functions:
                body = {n for n in lines if fn.region.body_start <= n <= fn.region.end_line}
                observations = [
                    o
                    for o in fn.view.observations
                    if nodeid in o.tests or o.caller == nodeid or o.example_test == nodeid
                ]
                if not body and not observations:
                    continue
                functions.append((fn, len(body), observations))
        functions_total = len(functions)
        functions = [
            (fn, body_lines, _param_reach(fn.view, observations))
            for fn, body_lines, observations in functions[:MAX_LISTED]
        ]
        message = entry.info.message
        if message and len(message) > MAX_MESSAGE_CHARS:
            message = message[:MAX_MESSAGE_CHARS] + "\n… (cut)"
        self.render(
            page,
            "test.html",
            kind="test",
            test=entry,
            message=message,
            files=files,
            functions=functions,
            functions_total=functions_total,
            strip=render_strip,
            data=None,
        )


# -- helpers ---------------------------------------------------------------------------------------


def _short(value: object, limit: int = 300) -> str:
    text = (
        value
        if isinstance(value, str)
        else json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _ranges(lines: Iterable[int]) -> str:
    """``3-5, 9, 12-13``."""
    out: list[str] = []
    start = prev = None
    for n in lines:
        if start is None:
            start = prev = n
        elif n == prev + 1:  # type: ignore[operator]
            prev = n
        else:
            out.append(f"{start}" if start == prev else f"{start}-{prev}")
            start = prev = n
    if start is not None:
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return ", ".join(out)


def _signature(region: FunctionRegion, view: FunctionView) -> str:
    """``validate_email(email: str) -> str`` from the region's parameters and their annotations."""
    parts = []
    for param in region.params:
        annotation = view.annotations.get(param.lstrip("*"))
        parts.append(f"{param}: {annotation}" if annotation else param)
    name = region.qualname.rsplit(".", 1)[-1]
    text = f"{name}({', '.join(parts)})"
    if view.returns:
        text += f" -> {view.returns}"
    return text


@dataclass(frozen=True, slots=True)
class Reach:
    """One parameter's strips on a test page."""

    param: str
    this_test: tuple[GridCell, ...]
    any_test: tuple[GridCell, ...]
    reached: tuple[str, ...]


def _param_reach(view: FunctionView, observations: list[Any]) -> list[Any]:
    """Per parameter: the classes this test passed in (filled) next to the function's never-exercised
    classes (hollow), and every expected class (filled when any test passed it).

    The live test page's ``param_reach`` when this build has it (so both pages draw the same strips),
    else the same rule here.
    """
    try:
        from coverset.web.facts_view import param_reach, reached_classes  # type: ignore[attr-defined]
    except ImportError:
        pass
    else:
        return list(param_reach(view, reached_classes(view, observations)))
    import dataclasses

    expected = {c.name for c in view.cov.expected}
    mine: dict[str, set[str]] = defaultdict(set)
    for obs in observations:
        if obs.param in ("<receiver>", "return"):
            continue
        mine[obs.param.lstrip("*")] |= view.catalogue.observed_classes([obs]) & expected
    missing = {c.name for c in view.cov.missing}
    params = [p.lstrip("*") for p in view.region.params]
    params += [c.param for c in view.cov.expected if c.param not in params]
    out = []
    for param in dict.fromkeys(params):
        cells = view.cells(param)
        if not cells:
            continue
        reached = mine.get(param, set())
        this_test = tuple(
            dataclasses.replace(cell, observed=cell.name in reached)
            for cell in cells
            if cell.name in reached or cell.name in missing
        )
        out.append(
            Reach(param, this_test, tuple(cells), tuple(sorted(c.short for c in cells if c.name in reached)))
        )
    return out


def default_out(source: Path | None) -> Path:
    """``<SOURCE>/report`` for a directory source, else ``./coverset-report``."""
    if source is not None and source.is_dir():
        return source / "report"
    return Path(os.getcwd()) / "coverset-report"
