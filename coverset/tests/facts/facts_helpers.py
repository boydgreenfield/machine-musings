"""Helpers for the facts-store tests, and the generator of ``fixtures/bundles/minimal``.

:func:`build_minimal_bundle` runs coverage.py 7.16 in-process (ctrace core, branch coverage, one
dynamic context per fake test, exactly as pytest-coverset does) over a tiny throwaway package,
writes ``coverage.json`` with ``json_report``, hand-writes the remaining bundle files in the formats
pytest-coverset writes (``shapes.jsonl.gz`` per the plan's Addendum B, ``invariant_links.jsonl``
rows keyed ``test_id``, ``collect.json`` as ``{"items", "errors"}``), and tars the lot into
``bundle.tar.gz``. :func:`measure` is the same coverage run over any source.

Regenerate the committed fixture with ``uv run python tests/facts/facts_helpers.py``.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import gzip
import importlib
import inspect
import json
import shutil
import sys
import tarfile
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import coverage

from coverset.facts.ingest import RunContext
from coverset.lang.base import IMPORT_CONTEXT, FunctionRegion, InputCov, NeutralBundle, ShapeObs, func_key

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = PROJECT_ROOT / "fixtures" / "bundles" / "minimal"
PACKAGE = "factspkg"
MODULE_PATH = f"src/{PACKAGE}/calc.py"

CALC_SOURCE = '''"""A tiny throwaway module measured by the facts fixture bundle."""


def add(a, b):
    return a + b


def classify(n):
    """Return the sign of ``n`` as a word."""
    if n < 0:
        return "neg"
    if n == 0:
        return "zero"
    return "pos"


def never_called(x):
    return x * 2


def excluded():  # pragma: no cover
    return "excluded"


class Box:
    def __init__(self, items):
        self.items = list(items)

    def total(self):
        total = 0
        for item in self.items:
            total += classify(item) and item
        return total
'''

TEST_FILE = "tests/test_calc.py"
T_ADD = f"{TEST_FILE}::test_add"
T_NEG = f"{TEST_FILE}::test_classify[neg]"
T_POS = f"{TEST_FILE}::test_classify[pos]"
T_BOX = f"{TEST_FILE}::test_box_total"
T_FAIL = f"{TEST_FILE}::test_zero_fails"
TESTS = (T_ADD, T_NEG, T_POS, T_BOX, T_FAIL)


CALLS: dict[str, Callable[[Any], object]] = {
    T_ADD: lambda calc: calc.add(1, 2),
    T_NEG: lambda calc: calc.classify(-3),
    T_POS: lambda calc: calc.classify(5),
    T_BOX: lambda calc: calc.Box([1, 2]).total(),
    T_FAIL: lambda calc: calc.classify(0),
}
"""What each fake test executes while its coverage context is active."""


@contextlib.contextmanager
def _isolated_import(project: Path, package: str) -> Iterator[None]:
    """Make ``project/src`` importable and forget the package afterwards."""
    sys.path.insert(0, str(project / "src"))
    try:
        with contextlib.chdir(project):
            yield
    finally:
        sys.path.remove(str(project / "src"))
        for name in [m for m in sys.modules if m == package or m.startswith(f"{package}.")]:
            del sys.modules[name]


def _firstlines(module: ModuleType) -> dict[str, int]:
    """``co_firstlineno`` of every function and method defined in ``module``, by qualname."""
    found: dict[str, int] = {}
    for obj in vars(module).values():
        if getattr(obj, "__module__", None) != module.__name__:
            continue
        members = vars(obj).values() if inspect.isclass(obj) else (obj,)
        for member in members:
            if inspect.isfunction(member):
                found[member.__qualname__] = member.__code__.co_firstlineno
    return found


def measure(
    dest: Path, source: str, calls: Mapping[str, Callable[[Any], object]], *, package: str = PACKAGE
) -> tuple[Path, dict[str, int]]:
    """Measure ``calls`` (nodeid -> f(module)) over ``source`` saved as ``src/<package>/calc.py``.

    Writes ``dest/repo/`` (the sources) and ``dest/bundle/`` (``coverage.sqlite``,
    ``coverage.json`` and an empty ``results.jsonl``); returns the bundle directory and each
    function's ``co_firstlineno`` by qualname.
    """
    project, bundle = dest / "repo", dest / "bundle"
    bundle.mkdir(parents=True)
    pkg_dir = project / "src" / package
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "__init__.py").write_text('"""Throwaway package for the facts fixture bundle."""\n')
    (pkg_dir / "calc.py").write_text(source)
    with _isolated_import(project, package):
        cov = coverage.Coverage(
            data_file=str(bundle / "coverage.sqlite"),
            data_suffix=False,
            branch=True,
            source=[f"src/{package}"],
        )
        cov.set_option("run:core", "ctrace")
        cov.set_option("run:relative_files", True)
        cov.start()
        module = importlib.import_module(f"{package}.calc")
        for nodeid, call in calls.items():
            cov.switch_context(nodeid)
            call(module)
        cov.stop()
        cov.save()
        cov.json_report(outfile=str(bundle / "coverage.json"))
        firstlines = _firstlines(module)
    (bundle / "results.jsonl").write_text("")
    return bundle, firstlines


def _shapes_rows(firstlines: dict[str, int]) -> list[dict]:
    funcs = [[MODULE_PATH, name, firstlines[name]] for name in ("add", "classify", "Box.total")]
    shapes = ["int|pos", "int|neg", "int|zero", f"obj:{PACKAGE}.calc.Box", "str|ascii|len:2-10"]
    tests = list(TESTS)
    callers = [
        {"kind": "test", "nodeid": T_ADD},
        {"kind": "test", "nodeid": T_NEG},
        {"kind": "test", "nodeid": T_POS},
        {"kind": "func", "path": MODULE_PATH, "qualname": "Box.total", "line": firstlines["Box.total"]},
        {"kind": "test", "nodeid": T_BOX},
        {"kind": "test", "nodeid": T_FAIL},
    ]
    header = {
        "kind": "header",
        "version": 1,
        "funcs": funcs,
        "shapes": shapes,
        "tests": tests,
        "callers": callers,
    }

    def obs(f: int, p: str, s: int, c: int, n: int, t: list[int], ex: str | None, ext: int | None) -> dict:
        return {
            "kind": "obs",
            "f": f,
            "p": p,
            "s": s,
            "c": c,
            "n": n,
            "t": t,
            "trunc": False,
            "ex": ex,
            "ext": ext,
        }

    return [
        header,
        obs(0, "a", 0, 0, 1, [0], "1", 0),
        obs(0, "b", 0, 0, 1, [0], "2", 0),
        obs(0, "return", 0, 0, 1, [0], "3", 0),
        obs(1, "n", 1, 1, 1, [1], "-3", 1),
        obs(1, "n", 0, 2, 1, [2], "5", 2),
        obs(1, "n", 0, 3, 2, [3], "1", 3),
        obs(1, "n", 2, 5, 1, [4], "0", 4),
        obs(1, "return", 4, 1, 1, [1], "'neg'", 1),
        obs(2, "<receiver>", 3, 4, 1, [3], None, None),
        {"kind": "func_sampling", "f": 0, "calls": 1, "tests": [0], "truncated": False},
        {"kind": "func_sampling", "f": 1, "calls": 5, "tests": [1, 2, 3, 4], "truncated": True},
        {"kind": "func_sampling", "f": 2, "calls": 1, "tests": [3], "truncated": False},
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def build_minimal_bundle(dest: Path) -> Path:
    """Build the minimal bundle under ``dest``: ``repo/`` (sources), ``bundle/`` and ``bundle.tar.gz``.

    Returns the extracted ``bundle`` directory.
    """
    bundle, firstlines = measure(dest, CALC_SOURCE, CALLS)
    with gzip.open(bundle / "shapes.jsonl.gz", "wt") as fh:
        fh.writelines(json.dumps(row) + "\n" for row in _shapes_rows(firstlines))
    outcomes = {T_FAIL: "failed"}
    _write_jsonl(
        bundle / "results.jsonl",
        [
            {
                "kind": "test",
                "nodeid": nodeid,
                "file": TEST_FILE,
                "outcome": outcomes.get(nodeid, "passed"),
                "duration_s": 0.01 * (i + 1),
                "failed_phase": "call" if nodeid in outcomes else None,
                "fail_sig": "c0ffee0123456789" if nodeid in outcomes else None,
                "longrepr": "AssertionError: assert 'zero' == 'pos'" if nodeid in outcomes else None,
                "start": 1_760_000_000.0 + i,
                "stop": 1_760_000_000.0 + i + 0.01 * (i + 1),
            }
            for i, nodeid in enumerate(TESTS)
        ],
    )
    collect: dict[str, list] = {
        "errors": [],
        "items": [
            {
                "nodeid": nodeid,
                "file": TEST_FILE,
                "line": 10 * (i + 1),
                "markers": ["parametrize"] if "[" in nodeid else [],
                "invariant_ids": [],
                "hypothesis": False,
            }
            for i, nodeid in enumerate(TESTS)
        ],
    }
    (bundle / "collect.json").write_text(json.dumps(collect, indent=1))
    (bundle / "durations.json").write_text(json.dumps({n: 0.01 * (i + 1) for i, n in enumerate(TESTS)}))
    _write_jsonl(bundle / "invariant_links.jsonl", [{"test_id": T_ADD, "invariant_id": "add_commutes"}])
    meta = {
        "generator": "tests/facts/facts_helpers.py",
        "coverage_version": coverage.__version__,
        "python": "{}.{}.{}".format(*sys.version_info[:3]),
        "plugin_version": "fixture",
        "tracer": "CTracer",
        "tool_id": 4,
    }
    (bundle / "meta.json").write_text(json.dumps(meta, indent=1))
    with tarfile.open(dest / "bundle.tar.gz", "w:gz") as tar:
        for path in sorted(bundle.iterdir()):
            tar.add(path, arcname=path.name)
    return bundle


def calc_functions(source: str = CALC_SOURCE, path: str = MODULE_PATH) -> list[FunctionRegion]:
    """Function regions of ``calc.py`` (a minimal stand-in for phase 4's ``index_functions``)."""
    regions: list[FunctionRegion] = []

    def visit(node: ast.AST, prefix: str, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.", True)
            elif isinstance(child, ast.FunctionDef):
                body = child.body
                if (
                    len(body) > 1
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                ):
                    body = body[1:]
                qualname = f"{prefix}{child.name}"
                regions.append(
                    FunctionRegion(
                        func_key=func_key(path, qualname, 0),
                        path=path,
                        qualname=qualname,
                        ordinal=0,
                        def_line=min([d.lineno for d in child.decorator_list] + [child.lineno]),
                        body_start=body[0].lineno,
                        end_line=child.end_lineno or child.lineno,
                        body_hash=f"body-{qualname}",
                        sig_hash=f"sig-{qualname}",
                        params=tuple(a.arg for a in child.args.args),
                        kind="method" if in_class else "function",
                    )
                )
                visit(child, f"{qualname}.<locals>.", False)

    visit(ast.parse(source), "", False)
    return regions


def split_bundle(bundle: NeutralBundle, nodeids: set[str], *, with_import: bool) -> NeutralBundle:
    """The part of ``bundle`` produced by ``nodeids`` (plus import-time lines when asked)."""
    keep = nodeids | ({IMPORT_CONTEXT} if with_import else set())
    return dataclasses.replace(
        bundle,
        tests=tuple(t for t in bundle.tests if t.nodeid in nodeids),
        coverage={n: by_path for n, by_path in bundle.coverage.items() if n in keep},
        shapes=tuple(o for o in bundle.shapes if set(o.tests) & nodeids),
        sampling=tuple(
            dataclasses.replace(s, tests=tuple(t for t in s.tests if t in nodeids))
            for s in bundle.sampling
            if set(s.tests) & nodeids
        ),
        durations={n: d for n, d in bundle.durations.items() if n in nodeids},
        meta={k: v for k, v in bundle.meta.items() if k != "convert"},
    )


INT_CLASSES = ("neg", "zero", "pos")


def int_catalogue(observations: list[ShapeObs]) -> dict[str, InputCov]:
    """A stand-in catalogue: int parameters expect neg/zero/pos."""
    seen: dict[str, set[str]] = defaultdict(set)
    for obs in observations:
        type_token, *features = obs.shape_key.split("|")
        if type_token == "int" and obs.param != "return":
            seen[obs.param].update(f for f in features if f in INT_CLASSES)
    return {
        param: InputCov(expected=INT_CLASSES, observed=tuple(sorted(found))) for param, found in seen.items()
    }


def make_ctx(run_id: str = "run-1", *, repo_id: int = 1, day: int = 1, **overrides: Any) -> RunContext:
    """A RunContext for tests."""
    values = {
        "repo_id": repo_id,
        "run_id": run_id,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "branch": "main",
        "pr_number": None,
        "created_at": datetime(2026, 10, day, 12, tzinfo=UTC),
        "scoring_basis_run_id": None,
    } | overrides
    return RunContext(**values)


if __name__ == "__main__":
    target = FIXTURE_DIR
    for child in ("bundle", "repo", "bundle.tar.gz"):
        path = target / child
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    target.mkdir(parents=True, exist_ok=True)
    build_minimal_bundle(target)
    print(f"wrote {target}")
