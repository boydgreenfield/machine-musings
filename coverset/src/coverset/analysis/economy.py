"""Test economy: what each test costs against what it alone covers.

A test suite is code too, and agent-written suites grow fast: a colleague watched an agent add about
95 lines of test scripting to check that one environment variable parses ``"true"``, ``"1"`` and
``"TRUE"``. This module puts numbers on that. Everything here is a pure function over in-memory
inputs (:class:`EconomyData`, built from a :class:`~coverset_model.neutral.NeutralBundle` or
straight from the Parquet columns; a way to read source text; optional base facts and history
callbacks). :mod:`coverset.facts.economy` feeds it from the facts store and a git mirror at
``finalize_run``; ``coverset report`` can call :func:`analyse` with a bundle and its
``SourceAccess``. The same code computes every number in both places.

The per-fact work is numpy over a CSR matrix of dense fact ids (one row per test), so a run with
20M ``test_lines`` rows (10,000 tests x 2,000 lines) analyses in about 6 s and ~50 bytes per row;
Python loops run per test, function, executed arc and shape observation, never per covered line.

What is measured (``docs/economy.md`` has every formula and caveat):

* **Test weight**, from the AST of the test file at head: statements in the test body, assertion
  count (``assert``, ``pytest.raises``/``warns``/``approx``/``fail``, ``self.assert*``,
  ``mock.assert_*``), fixtures requested (arguments plus ``autouse`` fixtures of the file, its class
  and its conftests), helper statements (bodies of the same-file, conftest and same-directory helper
  functions and fixtures the test reaches, each counted once per test, to depth 2) and the file's
  line count. A test whose file cannot be read or parsed has ``measured = False``: "not measured",
  never zero.
* **Coverage value**: production lines, branch arcs and observations it contributes, and the
  *unique* part of each (no other test in the run has it). A fact is one of: a line, an arc, an
  input class ``(function, param, class)``. ``unique facts = unique lines + unique arcs + 3 x unique
  classes`` (:data:`LINE_WEIGHT`, :data:`ARC_WEIGHT`, :data:`CLASS_WEIGHT`). Leverage is unique
  facts per test statement (own plus helper); cost per unique fact is milliseconds per unique fact.
* **Redundancy**: a signature per test (sha256 of its sorted line, arc and shape-observation ids;
  ids are dense and ordered by the facts' natural keys, so equal signatures mean equal fact sets),
  equivalence groups (identical signatures), dominance (A's facts a strict subset of B's and B at
  most :attr:`EconomyPolicy.dominance_slowdown` times slower), a greedy minimal covering set
  (``analysis/cover.py``, duration-weighted, then pruned to be irredundant) and parametrize
  collapse (cases versus distinct input-class flows per family). Tests outside the minimal set are
  **removable candidates**, never verdicts: their assertions may check things coverage cannot see.
* **Assertion-free tests**, **direct versus indirect testing** per function (only ever reached
  through other in-repo functions, or never observed at all), **unexercised exception handlers**,
  the **PR test tax** (added test statements per newly covered fact, flagged above a threshold)
  and **stale tests** (every production function a test covers changed since its file last did).

Branch arcs are not recorded per test (the bundle's ``arcs.jsonl`` is the run-wide union), so a
test's arcs are *inferred*: an executed arc ``a -> b`` is attributed to every test that executed
both ``a`` and ``b`` (or ``a`` alone, for an exit arc). That over-attributes when a test runs both
ends along different paths; it is documented as such and weighted no more than a line.
"""

from __future__ import annotations

import ast
import difflib
import functools
import hashlib
import posixpath
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import numpy as np

from coverset.analysis.cover import greedy_cover_csr, prune_csr
from coverset.analysis.functions import index_functions
from coverset.lang.base import (
    IMPORT_CONTEXT,
    FunctionRegion,
    FuncTriple,
    NeutralBundle,
    ShapeObs,
    StaticFile,
    func_key,
)
from coverset_model.catalogue import NON_INPUT_PARAMS, Catalogue

__all__ = [
    "ARC_WEIGHT",
    "CLASS_WEIGHT",
    "DEFAULT_DOMINANCE_SLOWDOWN",
    "DEFAULT_TEST_TAX_MIN_STATEMENTS",
    "DEFAULT_TEST_TAX_THRESHOLD",
    "LINE_WEIGHT",
    "MAX_DOMINANCE_CHECKS",
    "REMOVABLE_CAVEAT",
    "BaseSnapshot",
    "EconomyData",
    "EconomyPolicy",
    "EconomyReport",
    "FamilyEconomy",
    "FunctionTesting",
    "Handler",
    "NodeRef",
    "RunTotals",
    "SourceLookup",
    "StaleLookup",
    "TestEconomy",
    "TestTax",
    "TestWeight",
    "analyse",
    "analyse_data",
    "base_snapshot",
    "classes_of",
    "conftest_paths",
    "family_of",
    "find_handlers",
    "helper_module_candidates",
    "is_test_path",
    "observed_classes",
    "parse_nodeid",
    "statement_delta",
    "tax_message",
    "test_paths_of",
    "weigh_tests",
]

LINE_WEIGHT: Final = 1
"""Weight of one uniquely covered production line in ``unique_facts``."""
ARC_WEIGHT: Final = 1
"""Weight of one uniquely covered (inferred) branch arc."""
CLASS_WEIGHT: Final = 3
"""Weight of one uniquely observed input class ``(function, param, class)``: a class pins down a
behaviour a line cannot (``n:zero`` and ``n:negative`` run the same lines), and a function has far
fewer classes than lines, so one class is worth a few lines."""
DEFAULT_TEST_TAX_THRESHOLD: Final = 8.0
"""``policy.test_tax_threshold``: test statements added per newly covered fact above which a PR is
flagged."""
DEFAULT_TEST_TAX_MIN_STATEMENTS: Final = 20
"""``policy.test_tax_min_statements``: a PR adding fewer test statements is never flagged (one
nine-statement test for one new line is not a smell)."""
DEFAULT_DOMINANCE_SLOWDOWN: Final = 2.0
"""B dominates A only when B is at most this many times slower than A."""
REMOVABLE_CAVEAT: Final = (
    "Candidates only: coverage cannot see what a test asserts. A removable candidate covers nothing "
    "the other tests do not, but its assertions may still check something they do not."
)
"""Shown wherever removable candidates, equivalence groups or dominance appear (every surface)."""
HELPER_DEPTH: Final = 2
"""Helper calls are followed this deep: the test's own calls, and their calls."""

_PYTEST_ASSERTIONS: Final = frozenset({"raises", "warns", "approx", "deprecated_call", "fail"})
_RAISES: Final = frozenset({"raises", "warns", "deprecated_call"})
_FIXTURE_DECORATORS: Final = frozenset({"fixture", "yield_fixture"})
_NON_FIXTURE_ARGS: Final = frozenset({"self", "cls", "request"})
_TEST_DIRS: Final = frozenset({"tests", "test", "__tests__", "testing"})
_JS_TEST: Final = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_BODY_FIELDS: Final = frozenset({"body", "orelse", "finalbody", "handlers", "cases"})

type SourceLookup = Mapping[str, str] | Callable[[str], str | None]
"""Source text by repository-relative path: a mapping, or a reader returning None when unavailable."""
type StaleLookup = Callable[
    [Mapping[str, Sequence[FunctionRegion]]], Mapping[str, Mapping[str, str | None] | None]
]
"""Called once with ``{test file: head regions its tests cover}``; answers ``{test file: {func_key:
body_hash as of the test file's last change, None if the function did not exist then}}``, or None
for a test file whose history cannot be read. One call lets a git-backed lookup batch everything."""


def _reader(sources: SourceLookup | None) -> Callable[[str], str | None]:
    if sources is None:
        return lambda _path: None
    if isinstance(sources, Mapping):
        return sources.get
    return sources


# -- test ids and paths -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NodeRef:
    """A pytest nodeid split into file, enclosing classes, function name and parameter id."""

    path: str
    classes: tuple[str, ...]
    function: str
    params: str | None


def parse_nodeid(nodeid: str, file: str | None = None) -> NodeRef | None:
    """Split ``path::Class::func[params]``; None for ids that are not pytest's (Vitest's ``a > b``).

    The parameter id is cut at the first ``[`` before splitting on ``::`` (a parameter id may itself
    contain ``::``). ``file`` (the result row's repository-relative file) replaces the nodeid's path,
    which is relative to pytest's rootdir.
    """
    bracket = nodeid.find("[")
    base, params = (
        (nodeid[:bracket], nodeid[bracket + 1 :].removesuffix("]")) if bracket >= 0 else (nodeid, None)
    )
    parts = base.split("::")
    if len(parts) < 2 or not parts[-1]:
        return None
    return NodeRef(file or parts[0], tuple(parts[1:-1]), parts[-1], params)


def family_of(nodeid: str) -> str | None:
    """The parametrize family of a nodeid (the id without ``[...]``), None for an unparametrized one."""
    bracket = nodeid.find("[")
    return nodeid[:bracket] if bracket > 0 and nodeid.endswith("]") else None


def is_test_path(path: str, test_files: Collection[str] = ()) -> bool:
    """True for test code: a file holding a test of the run, ``conftest.py``, ``test_*.py``,
    ``*_test.py``, ``*.test.ts``/``*.spec.js`` and the like, or any file under a ``tests``,
    ``test``, ``testing`` or ``__tests__`` directory (helper modules such as ``tests/helpers.py``).

    Everything else measured is production code: only production facts count as coverage value.
    """
    if path in test_files:
        return True
    directory, _, name = path.rpartition("/")
    if (
        name == "conftest.py"
        or (name.startswith("test_") and name.endswith(".py"))
        or name.endswith("_test.py")
    ):
        return True
    if _JS_TEST.search(name):
        return True
    return any(part in _TEST_DIRS for part in directory.split("/") if part)


def conftest_paths(test_path: str) -> list[str]:
    """The ``conftest.py`` files pytest would load for ``test_path``, nearest first."""
    parts = test_path.split("/")[:-1]
    return ["/".join([*parts[:n], "conftest.py"]) for n in range(len(parts), -1, -1)]


def test_paths_of(bundle: NeutralBundle) -> dict[str, str]:
    """Each test's repository-relative file (the result row's ``file``, else the nodeid's path)."""
    files = {row.nodeid: row.file for row in bundle.tests if row.file}
    found: dict[str, str] = {}
    for nodeid in _test_ids(bundle):
        path = files.get(nodeid) or nodeid.split("::", 1)[0]
        found[nodeid] = path
    return found


test_paths_of.__test__ = False  # type: ignore[attr-defined]  # not a pytest test when imported


def _test_ids(bundle: NeutralBundle) -> list[str]:
    ids = {row.nodeid for row in bundle.tests} | set(bundle.coverage)
    ids.discard(IMPORT_CONTEXT)
    return sorted(ids)


# -- test weight (AST) --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TestWeight:
    """What a test costs to read: its statements, assertions, fixtures and helpers.

    Every count is None when ``measured`` is False (``reason`` says why): an unreadable file is
    "not measured", never a zero.
    """

    __test__ = False

    nodeid: str
    path: str
    measured: bool
    statements: int | None = None
    """Statements in the test function's body (nested statements included, docstring excluded)."""
    asserts: int | None = None
    """``assert`` statements plus pytest and unittest/mock assertion calls in the body."""
    helper_asserts: int | None = None
    """Assertions inside the helpers the test calls (not fixtures)."""
    raises: bool | None = None
    """The body checks an exception or warning (``pytest.raises``, ``assertRaises``, ...)."""
    fixtures: int | None = None
    """Fixtures requested by argument name plus autouse fixtures of the file, class and conftests."""
    helper_statements: int | None = None
    """Body statements of the helpers and fixtures the test reaches, each once, to depth 2."""
    helpers: tuple[str, ...] = ()
    """``path::name`` of every helper and fixture counted in ``helper_statements``."""
    file_lines: int | None = None
    """Lines of the test file."""
    reason: str | None = None

    @property
    def total_statements(self) -> int | None:
        """Own plus helper statements: the denominator of leverage."""
        if self.statements is None:
            return None
        return self.statements + (self.helper_statements or 0)


type _Def = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(slots=True)
class _TestModule:
    """The parts of one test-code file the weighing needs."""

    path: str
    n_lines: int
    functions: dict[str, _Def] = field(default_factory=dict)
    classes: dict[str, ast.ClassDef] = field(default_factory=dict)
    fixtures: dict[str, _Def] = field(default_factory=dict)
    autouse: list[str] = field(default_factory=list)
    pytest_names: set[str] = field(default_factory=set)
    imported: dict[str, str] = field(default_factory=dict)
    """Local name -> module name, for ``from <module> import <name>``."""


def _decorator_name(node: ast.expr) -> tuple[str, ast.Call | None]:
    call = node if isinstance(node, ast.Call) else None
    target = call.func if call is not None else node
    if isinstance(target, ast.Attribute):
        return target.attr, call
    if isinstance(target, ast.Name):
        return target.id, call
    return "", call


def _fixture_info(node: _Def) -> tuple[bool, bool]:
    """``(is a fixture, autouse)`` from the decorators."""
    for decorator in node.decorator_list:
        name, call = _decorator_name(decorator)
        if name in _FIXTURE_DECORATORS:
            autouse = call is not None and any(
                k.arg == "autouse" and isinstance(k.value, ast.Constant) and k.value.value is True
                for k in call.keywords
            )
            return True, autouse
    return False, False


def _parse_test_module(path: str, source: str) -> _TestModule | None:
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return None
    module = _TestModule(path, max(1, len(source.splitlines())))
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                local = alias.asname or alias.name
                if node.module == "pytest":
                    module.pytest_names.add(alias.name)
                elif node.level <= 1:
                    module.imported[local] = node.module
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            is_fixture, autouse = _fixture_info(node)
            if is_fixture:
                module.fixtures[node.name] = node
                if autouse:
                    module.autouse.append(node.name)
            else:
                module.functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            module.classes[node.name] = node
    return module


def _strip_docstring(body: Sequence[ast.stmt]) -> Sequence[ast.stmt]:
    first = body[0] if body else None
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    ):
        return body[1:]
    return body


def _count_statements(node: _Def) -> int:
    return sum(1 for stmt in _strip_docstring(node.body) for n in ast.walk(stmt) if isinstance(n, ast.stmt))


def _call_leaf(func: ast.expr) -> tuple[str, str | None]:
    """``(leaf name, receiver name)``: ``self.assertEqual`` -> ``("assertEqual", "self")``."""
    if isinstance(func, ast.Attribute):
        receiver = func.value.id if isinstance(func.value, ast.Name) else None
        return func.attr, receiver
    if isinstance(func, ast.Name):
        return func.id, None
    return "", None


def _assertions(node: _Def, pytest_names: Collection[str]) -> tuple[int, bool]:
    """``(assertion count, checks an exception or warning)`` in one function's body."""
    count, raises = 0, False
    for stmt in node.body:
        for n in ast.walk(stmt):
            if isinstance(n, ast.Assert):
                count += 1
            elif isinstance(n, ast.Call):
                leaf, receiver = _call_leaf(n.func)
                pytest_call = (receiver == "pytest" or (receiver is None and leaf in pytest_names)) and (
                    leaf in _PYTEST_ASSERTIONS
                )
                if pytest_call:
                    count += 1
                    raises = raises or leaf in _RAISES
                elif leaf.startswith("assert") or (leaf == "fail" and receiver == "self"):
                    count += 1
                    raises = raises or leaf.startswith(("assertRaises", "assertWarns"))
    return count, raises


def _parametrize_names(decorators: Iterable[ast.expr]) -> set[str]:
    names: set[str] = set()
    for decorator in decorators:
        leaf, call = _decorator_name(decorator)
        if leaf != "parametrize" or call is None or not call.args:
            continue
        first = call.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.update(part.strip() for part in first.value.split(",") if part.strip())
        elif isinstance(first, (ast.List, ast.Tuple)):
            names.update(
                e.value for e in first.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
            )
    return names


def _arg_names(node: _Def) -> list[str]:
    args = node.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


class _TestIndex:
    """Parsed test files, conftests and same-directory helper modules, read once each."""

    def __init__(self, read: Callable[[str], str | None]) -> None:
        self._read = read
        self._modules: dict[str, _TestModule | None] = {}
        self.read_paths: set[str] = set()
        """Every test-code path that was read successfully (for the PR statement diff)."""

    def module(self, path: str) -> _TestModule | None:
        if path not in self._modules:
            source = self._read(path)
            self._modules[path] = _parse_test_module(path, source) if source is not None else None
            if source is not None:
                self.read_paths.add(path)
        return self._modules[path]

    def conftests(self, test_path: str) -> list[_TestModule]:
        return [m for p in conftest_paths(test_path) if (m := self.module(p)) is not None]

    def helper_module(self, importer: str, module_name: str) -> _TestModule | None:
        """The same-directory (or repository-rooted) module ``from <module_name> import ...`` names."""
        for candidate in _module_candidates(importer, module_name):
            if (found := self.module(candidate)) is not None:
                return found
        return None


def _module_candidates(importer: str, module_name: str) -> list[str]:
    """Test-code files ``from <module_name> import ...`` in ``importer`` may name: the module beside
    the importer, then the module from the repository root (production modules are never helpers)."""
    relative = module_name.replace(".", "/") + ".py"
    candidates = (posixpath.join(posixpath.dirname(importer), relative), relative)
    return [c for c in dict.fromkeys(candidates) if is_test_path(c)]


def helper_module_candidates(path: str, source: str) -> list[str]:
    """Every file the weighing of the tests in ``path`` may read for imported helpers.

    A reader that fetches files in batches (a git mirror) asks for these up front, together with
    the test files and conftests, instead of one process per helper module.
    """
    module = _parse_test_module(path, source)
    if module is None:
        return []
    found: list[str] = []
    for module_name in dict.fromkeys(module.imported.values()):
        found.extend(_module_candidates(path, module_name))
    return list(dict.fromkeys(found))


def _find_class(module: _TestModule, names: Sequence[str]) -> ast.ClassDef | None:
    if not names:
        return None
    cls = module.classes.get(names[0])
    for name in names[1:]:
        if cls is None:
            return None
        cls = next((n for n in cls.body if isinstance(n, ast.ClassDef) and n.name == name), None)
    return cls


def _class_members(module: _TestModule, cls: ast.ClassDef, seen: set[str] | None = None) -> dict[str, _Def]:
    """Methods of ``cls`` and of its bases defined in the same module (nearest definition wins)."""
    seen = seen if seen is not None else set()
    if cls.name in seen:
        return {}
    seen.add(cls.name)
    members: dict[str, _Def] = {}
    for base in cls.bases:
        if isinstance(base, ast.Name) and (parent := module.classes.get(base.id)) is not None:
            members.update(_class_members(module, parent, seen))
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            members[node.name] = node
    return members


def _weigh_one(nodeid: str, path: str, index: _TestIndex) -> TestWeight:
    ref = parse_nodeid(nodeid, path)
    if ref is None:
        return TestWeight(nodeid, path, False, reason="not a pytest test id")
    if not path.endswith(".py"):
        return TestWeight(nodeid, path, False, reason="not a Python test file")
    module = index.module(path)
    if module is None:
        return TestWeight(nodeid, path, False, reason="test file unavailable or unparsable")
    cls = _find_class(module, ref.classes)
    members = _class_members(module, cls) if cls is not None else {}
    node = members.get(ref.function) if cls is not None else module.functions.get(ref.function)
    if node is None:
        return TestWeight(nodeid, path, False, file_lines=module.n_lines, reason="test function not found")

    conftests = index.conftests(path)
    statements = _count_statements(node)
    asserts, raises = _assertions(node, module.pytest_names)

    param_names = _parametrize_names(node.decorator_list)
    if cls is not None:
        param_names |= _parametrize_names(cls.decorator_list)
    class_fixtures: dict[str, _Def] = {}
    class_autouse: list[str] = []
    for name, member in members.items():
        is_fixture, autouse = _fixture_info(member)
        if is_fixture:
            class_fixtures[name] = member
            if autouse:
                class_autouse.append(name)

    def fixture_def(name: str) -> tuple[str, _Def] | None:
        if name in class_fixtures:
            return path, class_fixtures[name]
        for scope in (module, *conftests):
            if name in scope.fixtures:
                return scope.path, scope.fixtures[name]
        return None

    requested = [a for a in _arg_names(node) if a not in _NON_FIXTURE_ARGS and a not in param_names]
    autouse = [*class_autouse, *module.autouse, *(n for c in conftests for n in c.autouse)]
    fixtures = list(dict.fromkeys([*requested, *autouse]))

    counted: dict[str, int] = {}
    helper_asserts = 0
    frontier: list[tuple[str, _Def, _TestModule, bool]] = []
    for name in fixtures:
        if (found := fixture_def(name)) is not None:
            owner = index.module(found[0]) or module
            frontier.append((f"{found[0]}::{name}", found[1], owner, True))
    frontier += [(key, fn, mod, False) for key, fn, mod in _calls(node, module, members, index)]
    for depth in range(1, HELPER_DEPTH + 1):
        following: list[tuple[str, _Def, _TestModule, bool]] = []
        for key, fn, owner, is_fixture in frontier:
            if key in counted or fn is node:
                continue
            counted[key] = _count_statements(fn)
            if not is_fixture:
                helper_asserts += _assertions(fn, owner.pytest_names)[0]
            if depth == HELPER_DEPTH:
                continue
            following += [(k, f, m, False) for k, f, m in _calls(fn, owner, members, index)]
            if is_fixture:
                for arg in _arg_names(fn):
                    if (found := fixture_def(arg)) is not None:
                        following.append(
                            (f"{found[0]}::{arg}", found[1], index.module(found[0]) or owner, True)
                        )
        frontier = following

    return TestWeight(
        nodeid=nodeid,
        path=path,
        measured=True,
        statements=statements,
        asserts=asserts,
        helper_asserts=helper_asserts,
        raises=raises,
        fixtures=len(fixtures),
        helper_statements=sum(counted.values()),
        helpers=tuple(sorted(counted)),
        file_lines=module.n_lines,
    )


def _calls(
    node: _Def, module: _TestModule, members: Mapping[str, _Def], index: _TestIndex
) -> list[tuple[str, _Def, _TestModule]]:
    """Helpers ``node`` calls: same-module functions, ``self.<method>``, and functions imported from
    a same-directory test module (``from helpers import build``)."""
    found: list[tuple[str, _Def, _TestModule]] = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        if isinstance(func, ast.Name):
            if (fn := module.functions.get(func.id)) is not None:
                found.append((f"{module.path}::{func.id}", fn, module))
            elif (source_module := module.imported.get(func.id)) is not None:
                helper = index.helper_module(module.path, source_module)
                if helper is not None and (fn := helper.functions.get(func.id)) is not None:
                    found.append((f"{helper.path}::{func.id}", fn, helper))
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
            and (fn := members.get(func.attr)) is not None
            and not _fixture_info(fn)[0]
        ):
            found.append((f"{module.path}::self.{func.attr}", fn, module))
    return found


def weigh_tests(tests: Mapping[str, str], sources: SourceLookup | None) -> dict[str, TestWeight]:
    """:class:`TestWeight` of every ``nodeid -> test file`` in ``tests``, reading each file once."""
    index = _TestIndex(_reader(sources))
    return {nodeid: _weigh_one(nodeid, path, index) for nodeid, path in sorted(tests.items())}


# -- statement diff (PR test tax) ---------------------------------------------------------------


def _field_text(value: object) -> str:
    if isinstance(value, ast.AST):
        return ast.dump(value, annotate_fields=False)
    if isinstance(value, list):
        return "[" + ",".join(_field_text(v) for v in value) + "]"
    return repr(value)


def _statement_keys(source: str | None) -> Counter[str] | None:
    """A multiset of formatting-insensitive statement keys (bodies excluded), None if unparsable.

    A compound statement's key is its header (``for x in y``, ``def f(a)``, ``with m() as n``), so
    editing one line inside a 40-line test changes one key, not 40. Docstrings and other bare
    string expressions are not statements here.
    """
    if source is None:
        return Counter()
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    keys: Counter[str] = Counter()
    for node in ast.walk(tree):
        if not isinstance(node, ast.stmt):
            continue
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            continue
        parts = [_field_text(getattr(node, name, None)) for name in node._fields if name not in _BODY_FIELDS]
        keys[type(node).__name__ + ":" + "|".join(parts)] += 1
    return keys


def statement_delta(base: str | None, head: str | None) -> tuple[int, int] | None:
    """``(statements added, statements removed)`` between two versions of one file.

    Statements are compared as a multiset of keys (see :func:`_statement_keys`), so moving a test
    within or between functions adds nothing, a copy-pasted test adds every statement it copies, and
    reformatting adds nothing. None when either side does not parse. A missing side is an empty file.
    """
    old, new = _statement_keys(base), _statement_keys(head)
    if old is None or new is None:
        return None
    return sum((new - old).values()), sum((old - new).values())


# -- exception handlers -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Handler:
    """One ``except`` clause of production code."""

    path: str
    line: int
    """The ``except`` line."""
    body_line: int
    """The handler body's first statement: the line coverage must have executed."""
    types: tuple[str, ...]
    """Exception type names as written (``BaseException`` for a bare ``except:``)."""
    executed: bool | None = None
    """Whether any test (or import) executed ``body_line``; None when coverage is unknown."""
    func_key: str | None = None
    """Innermost function, class or module region holding the handler."""


def find_handlers(path: str, source: str) -> list[Handler]:
    """Every ``except``/``except*`` clause of ``source`` in source order (empty if unparsable)."""
    try:
        tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError, RecursionError):
        return []
    found: list[Handler] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Try, ast.TryStar)):
            continue
        for handler in node.handlers:
            if not handler.body:
                continue
            if handler.type is None:
                types: tuple[str, ...] = ("BaseException",)
            elif isinstance(handler.type, ast.Tuple):
                types = tuple(ast.unparse(e) for e in handler.type.elts)
            else:
                types = (ast.unparse(handler.type),)
            found.append(Handler(path, handler.lineno, handler.body[0].lineno, types))
    return sorted(found, key=lambda h: h.line)


# -- results ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EconomyPolicy:
    """The thresholds (``policy.*`` keys of the scoped settings when they exist)."""

    test_tax_threshold: float = DEFAULT_TEST_TAX_THRESHOLD
    test_tax_min_statements: int = DEFAULT_TEST_TAX_MIN_STATEMENTS
    dominance_slowdown: float = DEFAULT_DOMINANCE_SLOWDOWN


@dataclass(frozen=True, slots=True)
class TestEconomy:
    """One test's cost against its coverage value."""

    __test__ = False

    nodeid: str
    path: str
    family: str | None
    outcome: str | None
    weight: TestWeight
    cases: int
    """Size of the test's parametrize family (1 when unparametrized)."""
    lines: int
    arcs: int
    classes: int
    observations: int
    """Distinct ``(function, param, shape)`` observations (the signature's third set)."""
    unique_lines: int
    unique_arcs: int
    unique_classes: int
    unique_facts: int
    """``LINE_WEIGHT x unique_lines + ARC_WEIGHT x unique_arcs + CLASS_WEIGHT x unique_classes``."""
    leverage: float | None
    """``unique_facts / max(1, statements + helper_statements)``; None when not measured."""
    duration_ms: float | None
    cost_per_fact_ms: float | None
    """``duration_ms / unique_facts``; None without unique facts or a duration."""
    signature: str
    group_id: str | None
    """Shared by every test with the same non-empty signature (None when the test is alone)."""
    dominated_by: str | None
    in_minimal_set: bool
    removable: bool
    """Outside the minimal covering set, with facts, and not skipped: a removable *candidate*."""
    assertion_free: bool | None
    stale: bool | None
    empty: bool
    """No production line, arc or observation at all (nothing measured to compare)."""

    @property
    def measured(self) -> bool:
        """Whether the test's weight was read from its source."""
        return self.weight.measured


@dataclass(frozen=True, slots=True)
class FamilyEconomy:
    """One parametrized test family: cases against the distinct flows they reach."""

    family: str
    path: str
    cases: int
    distinct_flows: int | None
    """Distinct sets of ``(function, param, input class)`` among the cases; None when no case was
    observed reaching any traced function."""
    distinct_signatures: int
    case_ids: tuple[str, ...]

    @property
    def redundant_cases(self) -> int:
        """Cases beyond the distinct flows (signatures when flows are unknown)."""
        distinct = self.distinct_flows if self.distinct_flows is not None else self.distinct_signatures
        return max(0, self.cases - distinct)


@dataclass(frozen=True, slots=True)
class FunctionTesting:
    """How one production function is reached by the tests."""

    func_key: str
    path: str
    qualname: str
    kind: str
    covered: bool
    """Some test (not import) executed a line of its body."""
    observed: bool
    """The shape tracer recorded at least one call with a caller."""
    direct_tests: int
    """Tests observed calling it directly."""
    indirect_tests: int
    """Tests whose calls reached it only through other in-repo functions."""
    indirect_only: bool
    """Observed, never called by a test directly."""
    untested: bool
    """Neither covered nor observed."""
    handlers_total: int | None
    handlers_unexercised: int | None
    handlers: tuple[Handler, ...] = ()
    """The unexercised handlers (``executed`` False)."""


@dataclass(frozen=True, slots=True)
class TestTax:
    """The PR test tax: test statements added per newly covered fact."""

    __test__ = False

    base_run_id: str | None
    base_sha: str | None
    base_exact: bool
    """The base facts come from a run of exactly the PR's base commit."""
    added_statements: int
    removed_statements: int
    test_files_changed: int
    newly_covered_lines: int
    new_classes: int
    new_tests: int
    new_tests_unique_facts: int
    tax: float
    """``added_statements / max(1, newly_covered_lines + CLASS_WEIGHT x new_classes)``."""
    threshold: float
    min_statements: int
    flagged: bool
    message: str


@dataclass(frozen=True, slots=True)
class RunTotals:
    """Run-level totals (the one ``run_economy`` row, without the tax)."""

    tests: int
    measured_tests: int
    statements: int
    helper_statements: int
    asserts: int
    assertion_free: int
    lines: int
    arcs: int
    classes: int
    observations: int
    unique_facts: int
    total_duration_ms: float
    minimal_set_size: int
    minimal_set_duration_ms: float
    removable_candidates: int
    removable_duration_ms: float
    equivalence_groups: int
    tests_in_groups: int
    dominated: int
    empty_tests: int
    families: int
    collapsible_families: int
    redundant_cases: int
    stale_tests: int | None
    functions: int
    indirect_only_functions: int
    untested_functions: int
    handlers_total: int | None
    handlers_unexercised: int | None


@dataclass(frozen=True, slots=True)
class EconomyReport:
    """Everything :func:`analyse` computed for one run."""

    tests: tuple[TestEconomy, ...]
    families: tuple[FamilyEconomy, ...]
    functions: tuple[FunctionTesting, ...]
    totals: RunTotals
    tax: TestTax | None
    notes: tuple[str, ...] = ()
    """Why parts were not measured (no sources, no base run, no history)."""
    weights: Mapping[str, int] = field(
        default_factory=lambda: {"line": LINE_WEIGHT, "arc": ARC_WEIGHT, "class": CLASS_WEIGHT}
    )

    def by_nodeid(self) -> dict[str, TestEconomy]:
        """Tests by nodeid."""
        return {t.nodeid: t for t in self.tests}

    def groups(self) -> dict[str, list[str]]:
        """Equivalence groups: group id -> nodeids, largest first."""
        found: dict[str, list[str]] = defaultdict(list)
        for test in self.tests:
            if test.group_id is not None:
                found[test.group_id].append(test.nodeid)
        return dict(sorted(found.items(), key=lambda kv: (-len(kv[1]), kv[1][0])))

    def removable(self) -> list[TestEconomy]:
        """Removable candidates, slowest first."""
        return sorted(
            (t for t in self.tests if t.removable), key=lambda t: (-(t.duration_ms or 0.0), t.nodeid)
        )


# -- base facts (PR test tax) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BaseSnapshot:
    """What the PR test tax compares head against: the base run's coverage, classes and sources."""

    run_id: str | None
    sha: str | None
    exact: bool
    coverage: Mapping[str, frozenset[int]]
    """Production path -> lines any test (not import) covered in the base run."""
    statements: Mapping[str, frozenset[int]]
    classes: frozenset[tuple[str, str]]
    """``(func_key, "param:class")`` observed in the base run."""
    tests: frozenset[str]
    test_sources: Mapping[str, str]
    """Test-code files at the base commit (absent files are simply missing)."""
    sources: Mapping[str, str] = field(default_factory=dict)
    """Production files at the base commit, for mapping lines across the diff (optional)."""


class _Resolver:
    """Shape-tracer function triples -> ``func_key`` (the same rule as ``facts.ingest``)."""

    def __init__(self, functions: Sequence[FunctionRegion]) -> None:
        self.by_triple = {r.triple: r for r in functions}
        by_name: dict[tuple[str, str], list[FunctionRegion]] = defaultdict(list)
        for region in functions:
            by_name[(region.path, region.qualname)].append(region)
        self.unique = {name: rs[0] for name, rs in by_name.items() if len(rs) == 1}

    def key(self, triple: FuncTriple) -> str:
        region = self.by_triple.get(triple) or self.unique.get((triple[0], triple[1]))
        return region.func_key if region is not None else func_key(triple[0], triple[1], 0)


@functools.lru_cache(maxsize=65536)
def classes_of(param: str, shape_key: str) -> tuple[str, ...]:
    """Input classes (``param:class``) one shape falls into, without declared types (none for
    ``return`` and ``<receiver>``): the third kind of fact."""
    if param in NON_INPUT_PARAMS:
        return ()
    obs = ShapeObs(("", "", 0), param, shape_key, "", 1, (), False, None, None)
    return tuple(sorted(_CATALOGUE.observed_classes([obs])))


_CATALOGUE: Final = Catalogue()


def observed_classes(
    shapes: Iterable[ShapeObs], functions: Sequence[FunctionRegion], test_files: Collection[str] = ()
) -> frozenset[tuple[str, str]]:
    """Every ``(func_key, "param:class")`` some test observed, by the same rules as the head side of
    :func:`analyse_data` (functions in test code skipped, observations without tests ignored)."""
    resolver = _Resolver(functions)
    found: set[tuple[str, str]] = set()
    for obs in shapes:
        if not obs.tests or is_test_path(obs.func[0], test_files):
            continue
        key = resolver.key(obs.func)
        found.update((key, cls) for cls in classes_of(obs.param, obs.shape_key))
    return frozenset(found)


def base_snapshot(
    bundle: NeutralBundle,
    *,
    run_id: str | None = None,
    sha: str | None = None,
    exact: bool = True,
    functions: Sequence[FunctionRegion] | None = None,
    test_sources: Mapping[str, str] | None = None,
    sources: Mapping[str, str] | None = None,
) -> BaseSnapshot:
    """The :class:`BaseSnapshot` of a base run's bundle (``coverset report --compare`` has one)."""
    test_files = set(test_paths_of(bundle).values())
    coverage: dict[str, set[int]] = defaultdict(set)
    for nodeid, by_path in bundle.coverage.items():
        if nodeid == IMPORT_CONTEXT:
            continue
        for path, lines in by_path.items():
            if not is_test_path(path, test_files):
                coverage[path].update(lines)
    classes = observed_classes(
        bundle.shapes, functions if functions is not None else bundle.functions, test_files
    )
    return BaseSnapshot(
        run_id=run_id,
        sha=sha,
        exact=exact,
        coverage={p: frozenset(v) for p, v in coverage.items()},
        statements={p: frozenset(s.statements) for p, s in bundle.static.items()},
        classes=classes,
        tests=frozenset(_test_ids(bundle)),
        test_sources=dict(test_sources or {}),
        sources=dict(sources or {}),
    )


# -- the analysis -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, eq=False)
class EconomyData:
    """One run's inputs in array form: what :func:`analyse_data` reads.

    Coverage is three aligned arrays, one row per ``(test, file, line)`` executed: ``cov_test`` is
    the index into ``tests`` (``-1`` for the ``<import>`` pseudo test), ``cov_file`` a key of
    ``paths``. Built from a bundle by :meth:`from_bundle`, or straight from the Parquet columns by
    :func:`coverset.facts.economy.economy_data` (no per-line Python objects, so a 20M-row run fits).
    """

    tests: tuple[str, ...]
    """Nodeids in sorted order (their index is the test index)."""
    test_files: tuple[str, ...]
    outcomes: tuple[str | None, ...]
    durations_ms: tuple[float | None, ...]
    paths: Mapping[int, str]
    cov_test: np.ndarray
    cov_file: np.ndarray
    cov_line: np.ndarray
    static: Mapping[str, StaticFile]
    run_arcs: Mapping[str, Sequence[tuple[int, int]]]
    shapes: Sequence[ShapeObs]
    functions: Sequence[FunctionRegion] = ()

    @classmethod
    def from_bundle(cls, bundle: NeutralBundle, durations: Mapping[str, float] | None = None) -> EconomyData:
        """The arrays of a bundle (``durations``, nodeid -> seconds, override the bundle's own)."""
        files = test_paths_of(bundle)
        tests = tuple(sorted(files))
        index = {n: i for i, n in enumerate(tests)}
        outcomes = {row.nodeid: row.outcome for row in bundle.tests}
        seconds: dict[str, float] = {row.nodeid: row.duration_s for row in bundle.tests}
        seconds.update(bundle.durations)
        seconds.update(durations or {})
        path_ids: dict[str, int] = {}
        test_col: list[np.ndarray] = []
        file_col: list[np.ndarray] = []
        line_col: list[np.ndarray] = []
        for nodeid, by_path in bundle.coverage.items():
            test_index = -1 if nodeid == IMPORT_CONTEXT else index.get(nodeid)
            if test_index is None:
                continue
            for path, lines in by_path.items():
                file_index = path_ids.setdefault(path, len(path_ids))
                arr = np.unique(np.asarray(lines, dtype=np.int32))
                line_col.append(arr)
                file_col.append(np.full(arr.size, file_index, dtype=np.int32))
                test_col.append(np.full(arr.size, test_index, dtype=np.int32))
        for path in bundle.static:
            path_ids.setdefault(path, len(path_ids))
        empty = np.empty(0, dtype=np.int32)
        return cls(
            tests=tests,
            test_files=tuple(files[n] for n in tests),
            outcomes=tuple(outcomes.get(n) for n in tests),
            durations_ms=tuple(seconds[n] * 1000.0 if n in seconds else None for n in tests),
            paths={i: p for p, i in path_ids.items()},
            cov_test=np.concatenate(test_col) if test_col else empty,
            cov_file=np.concatenate(file_col) if file_col else empty,
            cov_line=np.concatenate(line_col) if line_col else empty,
            static=bundle.static,
            run_arcs=bundle.run_arcs,
            shapes=bundle.shapes,
            functions=bundle.functions,
        )


def _index_regions(
    data: EconomyData,
    functions: Sequence[FunctionRegion] | None,
    read: Callable[[str], str | None],
    prod: Collection[str],
) -> list[FunctionRegion]:
    if functions:
        return list(functions)
    if data.functions:
        return list(data.functions)
    regions: list[FunctionRegion] = []
    for path in sorted(prod):
        if path.endswith(".py") and (source := read(path)) is not None:
            regions.extend(index_functions(path, source))
    return regions


def _line_map(base: str, head: str) -> dict[int, int]:
    """Head line -> base line for every line the diff leaves unchanged (1-based)."""
    matcher = difflib.SequenceMatcher(None, base.splitlines(), head.splitlines(), autojunk=False)
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, _ in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[j1 + offset + 1] = i1 + offset + 1
    return mapping


def tax_message(added: int, lines: int, classes: int) -> str:
    """``"95 test statements added for 3 newly covered lines and 1 input class"``."""

    def plural(n: int, word: str, many: str) -> str:
        return f"{n:,} {word if n == 1 else many}"

    return (
        f"{plural(added, 'test statement', 'test statements')} added for "
        f"{plural(lines, 'newly covered line', 'newly covered lines')} and "
        f"{plural(classes, 'input class', 'input classes')}"
    )


class _FactMatrix:
    """Every test's facts as a CSR matrix of dense fact ids, in canonical order per kind.

    Fact ids are laid out kind by kind: lines ``[0, n_lines)``, then arcs, then shape observations,
    then input classes, each kind sorted by its natural key (path, line; path, from, to; function,
    param, shape; function, class), so a test's sorted ids identify its fact set independently of
    how the inputs were ordered. ``offsets`` index ``facts`` per test; ``holders`` counts the tests
    holding each fact.
    """

    def __init__(self, n_tests: int, kinds: Sequence[int], pairs: list[np.ndarray]) -> None:
        """``pairs`` is ``[test indices, fact ids]``, one entry per (test, fact); the list is emptied
        so the two arrays (the biggest temporaries of a run) are freed before the sort."""
        pair_fact, pair_test = pairs.pop(), pairs.pop()
        self.bounds = np.cumsum([0, *kinds])  # kind k occupies [bounds[k], bounds[k + 1])
        total = int(self.bounds[-1])
        width = max(total, 1)
        key = pair_test.astype(np.int64) * width
        key += pair_fact
        del pair_test, pair_fact
        key = _sorted_unique(key, inplace=True)
        self.offsets = np.searchsorted(key, np.arange(n_tests + 1, dtype=np.int64) * width).astype(np.int64)
        self.test = (key // width).astype(np.int32)
        self.facts = (key % width).astype(np.int32)
        del key
        self.holders = np.bincount(self.facts, minlength=total).astype(np.int32)
        self.kind = (np.searchsorted(self.bounds, self.facts, side="right") - 1).astype(np.int8)
        self.n_tests = n_tests

    def segment(self, i: int) -> np.ndarray:
        return self.facts[self.offsets[i] : self.offsets[i + 1]]

    def per_test(self, mask: np.ndarray) -> np.ndarray:
        """How many of each test's facts satisfy ``mask`` (aligned with ``facts``)."""
        return np.bincount(self.test[mask], minlength=self.n_tests)

    def signature_end(self, i: int) -> int:
        """End (exclusive) of test ``i``'s signature facts (lines, arcs, observations)."""
        segment = self.segment(i)
        return int(self.offsets[i] + np.searchsorted(segment, self.bounds[3]))


def _sorted_unique(values: np.ndarray, *, inplace: bool = False) -> np.ndarray:
    """``np.unique`` for int arrays by sort plus neighbour mask: several times faster than the hash
    path numpy 2 takes for large int64 arrays, and the sort is needed anyway. ``inplace`` sorts a
    temporary the caller no longer needs instead of copying it."""
    if inplace:
        values.sort()
        ordered = values
    else:
        ordered = np.sort(values)
    if ordered.size < 2:
        return ordered
    keep = np.empty(ordered.size, dtype=bool)
    keep[0] = True
    np.not_equal(ordered[1:], ordered[:-1], out=keep[1:])
    return ordered if keep.all() else ordered[keep]


_LINES, _ARCS, _OBS, _CLASSES = 0, 1, 2, 3
_LINE_BITS: Final = 32
MAX_DOMINANCE_CHECKS: Final = 2_000_000
"""Subset checks one run may spend on dominance; beyond it dominance is partial (noted)."""


def analyse(
    bundle: NeutralBundle,
    sources: SourceLookup | None = None,
    *,
    functions: Sequence[FunctionRegion] | None = None,
    durations: Mapping[str, float] | None = None,
    base: BaseSnapshot | None = None,
    stale_lookup: StaleLookup | None = None,
    policy: EconomyPolicy | None = None,
) -> EconomyReport:
    """The test economy of one run's bundle (see :func:`analyse_data` for the arguments)."""
    return analyse_data(
        EconomyData.from_bundle(bundle, durations),
        sources,
        functions=functions,
        base=base,
        stale_lookup=stale_lookup,
        policy=policy,
    )


def analyse_data(
    data: EconomyData,
    sources: SourceLookup | None = None,
    *,
    functions: Sequence[FunctionRegion] | None = None,
    base: BaseSnapshot | None = None,
    stale_lookup: StaleLookup | None = None,
    policy: EconomyPolicy | None = None,
) -> EconomyReport:
    """The test economy of one run.

    ``sources`` reads head files (test files, conftests, helper modules, and production files for
    the exception handlers and, for a run without shipped regions, the function index).
    ``functions`` are the head's function regions (else the data's, else indexed from
    ``sources``). ``base`` enables the PR test tax, ``stale_lookup`` stale tests. Missing inputs
    make the corresponding numbers None and add a line to ``notes``; nothing raises for missing
    data. Every per-fact step is a numpy operation over the coverage arrays; Python loops run per
    test, per function, per executed arc and per shape observation only.
    """
    policy = policy or EconomyPolicy()
    read = _reader(sources)
    notes: list[str] = []
    tests = data.tests
    n = len(tests)
    index = {nodeid: i for i, nodeid in enumerate(tests)}
    test_files = set(data.test_files)

    # Production files and their canonical rank (path order).
    candidates = {*data.paths.values(), *data.static}
    prod_paths = sorted(p for p in candidates if not is_test_path(p, test_files))
    rank_of = {p: r for r, p in enumerate(prod_paths)}
    max_file = max(data.paths, default=-1) + 1
    file_rank = np.full(max_file, -1, dtype=np.int32)
    for file_index, path in data.paths.items():
        file_rank[file_index] = rank_of.get(path, -1)
    regions = _index_regions(data, functions, read, prod_paths)
    resolver = _Resolver(regions)

    # Lines: dense ids over (rank, line) of production rows; import rows only feed "executed".
    # (Intermediates are deleted as soon as they are used: at 20M rows each int64 column is 160 MB.)
    row_rank = file_rank[data.cov_file] if data.cov_file.size else np.empty(0, dtype=np.int32)
    prod_rows = row_rank >= 0
    executed_keys = _sorted_unique(
        (row_rank[prod_rows].astype(np.int64) << _LINE_BITS) | data.cov_line[prod_rows].astype(np.int64),
        inplace=True,
    )
    test_rows = prod_rows & (data.cov_test >= 0)
    del prod_rows
    line_test = data.cov_test[test_rows].astype(np.int32)
    line_key = (row_rank[test_rows].astype(np.int64) << _LINE_BITS) | data.cov_line[test_rows].astype(
        np.int64
    )
    del row_rank, test_rows
    line_keys = _sorted_unique(line_key)
    line_fact = np.searchsorted(line_keys, line_key).astype(np.int32)
    del line_key
    n_lines = int(line_keys.size)
    # Tests by line fact (sorted test indices per line), for arcs, functions and stale tests.
    width = max(n, 1)
    by_line = line_fact.astype(np.int64) * width
    by_line += line_test
    by_line = _sorted_unique(by_line, inplace=True)
    line_starts = np.searchsorted(by_line, np.arange(n_lines + 1, dtype=np.int64) * width)
    by_line_test = (by_line % width).astype(np.int32)
    del by_line

    def line_id(rank: int, line: int) -> int | None:
        key = (rank << _LINE_BITS) | line
        i = int(np.searchsorted(line_keys, key))
        return i if i < n_lines and int(line_keys[i]) == key else None

    def holders_of_line(fact: int) -> np.ndarray:
        return by_line_test[line_starts[fact] : line_starts[fact + 1]]

    # Arcs, inferred: an executed arc a -> b belongs to every test that ran a and b (a, for exits).
    arc_keys: list[tuple[int, int, int]] = []
    arc_tests: list[np.ndarray] = []
    for path, arcs in data.run_arcs.items():
        rank = rank_of.get(path)
        if rank is None:
            continue
        for a, b in sorted(set(arcs)):
            fa = line_id(rank, a) if a > 0 else None
            if fa is None:
                continue
            holders = holders_of_line(fa)
            if b > 0:
                fb = line_id(rank, b)
                holders = (
                    np.intersect1d(holders, holders_of_line(fb), assume_unique=True)
                    if fb is not None
                    else holders[:0]
                )
            if holders.size:
                arc_keys.append((rank, a, b))
                arc_tests.append(holders)
    arc_order = sorted(range(len(arc_keys)), key=arc_keys.__getitem__)
    n_arcs = len(arc_keys)

    # Shape observations and input classes, and who calls each function.
    obs_pairs: dict[tuple[str, str, str], set[int]] = defaultdict(set)
    class_pairs: dict[tuple[str, str], set[int]] = defaultdict(set)
    direct: dict[str, set[str]] = defaultdict(set)
    indirect: dict[str, set[str]] = defaultdict(set)
    for obs in data.shapes:
        if is_test_path(obs.func[0], test_files):
            continue
        key = resolver.key(obs.func)
        holders_ids = {index[t] for t in obs.tests if t in index}
        if isinstance(obs.caller, str):
            direct[key].update(obs.tests)
            direct[key].add(obs.caller)
        elif is_test_path(obs.caller[0], test_files):
            direct[key].update(obs.tests)  # a test helper calling it is the test calling it
        else:
            indirect[key].update(obs.tests)
        obs_pairs[(key, obs.param, obs.shape_key)].update(holders_ids)
        for cls in classes_of(obs.param, obs.shape_key):
            class_pairs[(key, cls)].update(holders_ids)
    obs_keys = sorted(k for k, v in obs_pairs.items() if v)
    class_keys = sorted(k for k, v in class_pairs.items() if v)

    # The matrix.
    kinds = (n_lines, n_arcs, len(obs_keys), len(class_keys))
    offs = np.cumsum([0, *kinds])
    pt: list[np.ndarray] = [line_test.astype(np.int32)]
    pf: list[np.ndarray] = [line_fact]
    for dense, i in enumerate(arc_order):
        pt.append(arc_tests[i])
        pf.append(np.full(arc_tests[i].size, offs[1] + dense, dtype=np.int32))
    holder_sets = [*(obs_pairs[k] for k in obs_keys), *(class_pairs[k] for k in class_keys)]
    for dense, holder_set in enumerate(holder_sets):
        ids = np.fromiter(sorted(holder_set), dtype=np.int32, count=len(holder_set))
        pt.append(ids)
        pf.append(np.full(ids.size, offs[2] + dense, dtype=np.int32))
    pairs = [np.concatenate(pt), np.concatenate(pf)]
    del pt, pf, line_test, line_fact, arc_tests
    matrix = _FactMatrix(n, kinds, pairs)

    unique_mask = matrix.holders[matrix.facts] == 1
    count = {k: matrix.per_test(matrix.kind == k) for k in (_LINES, _ARCS, _OBS, _CLASSES)}
    unique = {k: matrix.per_test(unique_mask & (matrix.kind == k)) for k in (_LINES, _ARCS, _CLASSES)}

    # Weights and durations.
    weights = (
        weigh_tests(dict(zip(tests, data.test_files, strict=True)), read)
        if sources is not None
        else {
            nodeid: TestWeight(nodeid, path, False, reason="no sources")
            for nodeid, path in zip(tests, data.test_files, strict=True)
        }
    )
    if sources is None:
        notes.append("test weights not measured: no source files were available")
    else:
        reasons = Counter(w.reason for w in weights.values() if not w.measured)
        if reasons:
            detail = ", ".join(f"{reason} ({count:,})" for reason, count in reasons.most_common())
            notes.append(f"test weights not measured for {sum(reasons.values()):,} of {n:,} tests: {detail}")
    known = [d for d in data.durations_ms if d is not None and d > 0]
    median_ms = statistics.median(known) if known else 1.0
    dur = np.array([d if d is not None and d > 0 else median_ms for d in data.durations_ms], dtype=np.float64)

    # Signatures and equivalence groups (over lines, arcs and observations).
    sig_end = np.array([matrix.signature_end(i) for i in range(n)], dtype=np.int64)
    sig_size = sig_end - matrix.offsets[:-1]
    signatures = [
        hashlib.sha256(matrix.facts[matrix.offsets[i] : sig_end[i]].astype("<i8").tobytes()).hexdigest()
        for i in range(n)
    ]
    by_signature: dict[str, list[int]] = defaultdict(list)
    for i in range(n):
        if sig_size[i]:
            by_signature[signatures[i]].append(i)
    group_of = {i: sig[:12] for sig, members in by_signature.items() if len(members) > 1 for i in members}

    # The signature facts as their own CSR (for cover and dominance).
    sig_mask = matrix.kind < _CLASSES
    sig_facts = matrix.facts[sig_mask]
    sig_test = matrix.test[sig_mask]
    sig_offsets = np.searchsorted(sig_test, np.arange(n + 1)).astype(np.int64)
    by_fact = sig_facts.astype(np.int64) * width  # CSC: tests by fact
    by_fact += sig_test
    by_fact.sort()
    fact_starts = np.searchsorted(by_fact, np.arange(int(offs[3]) + 1, dtype=np.int64) * width)
    holders_test = (by_fact % width).astype(np.int32)
    del by_fact, sig_test

    # Dominance.
    dominated_by: dict[int, int] = {}
    checks = 0
    partial = False
    for a in range(n):
        size_a = int(sig_size[a])
        if not size_a:
            continue
        seg_a = sig_facts[sig_offsets[a] : sig_offsets[a + 1]]
        counts_a = matrix.holders[seg_a]
        picks = np.argpartition(counts_a, 2)[:3] if seg_a.size > 3 else np.arange(seg_a.size)
        rarest = seg_a[picks[np.argsort(counts_a[picks], kind="stable")]]
        cand = holders_test[fact_starts[rarest[0]] : fact_starts[rarest[0] + 1]]
        for f in rarest[1:]:
            if cand.size <= 1:
                break
            cand = np.intersect1d(cand, holders_test[fact_starts[f] : fact_starts[f + 1]], assume_unique=True)
        cand = cand[(sig_size[cand] > size_a) & (dur[cand] <= policy.dominance_slowdown * dur[a])]
        if not cand.size:
            continue
        for b in sorted(cand.tolist(), key=lambda b: (dur[b], -sig_size[b], b)):
            checks += 1
            if checks > MAX_DOMINANCE_CHECKS:
                partial = True
                break
            seg_b = sig_facts[sig_offsets[b] : sig_offsets[b + 1]]
            pos = np.searchsorted(seg_b, seg_a)
            if bool(np.all(pos < seg_b.size)) and bool(
                np.array_equal(seg_b[np.minimum(pos, seg_b.size - 1)], seg_a)
            ):
                dominated_by[a] = b
                break
        if partial:
            notes.append(f"dominance partly computed: over {MAX_DOMINANCE_CHECKS:,} subset checks")
            break

    # The minimal covering set.
    seconds = dur / 1000.0
    minimal = set(
        prune_csr(greedy_cover_csr(sig_offsets, sig_facts, seconds), sig_offsets, sig_facts, seconds)
    )

    # Families.
    class_sig = [
        hashlib.sha256(matrix.facts[sig_end[i] : matrix.offsets[i + 1]].astype("<i8").tobytes()).digest()
        for i in range(n)
    ]
    families_of: dict[str, list[int]] = defaultdict(list)
    for i, nodeid in enumerate(tests):
        if (fam := family_of(nodeid)) is not None:
            families_of[fam].append(i)
    family_rows: list[FamilyEconomy] = []
    for fam, members in sorted(families_of.items()):
        has_classes = any(matrix.offsets[i + 1] > sig_end[i] for i in members)
        family_rows.append(
            FamilyEconomy(
                family=fam,
                path=data.test_files[members[0]],
                cases=len(members),
                distinct_flows=len({class_sig[i] for i in members}) if has_classes else None,
                distinct_signatures=len({signatures[i] for i in members}),
                case_ids=tuple(tests[i] for i in members),
            )
        )
    case_count = {i: len(members) for members in families_of.values() for i in members}

    # Functions: covered, observed, direct or indirect, handlers.
    regions_by_path: dict[str, list[FunctionRegion]] = defaultdict(list)
    for region in regions:
        if region.path in rank_of:
            regions_by_path[region.path].append(region)

    def line_range(region: FunctionRegion) -> tuple[int, int]:
        rank = rank_of[region.path]
        lo = int(np.searchsorted(line_keys, (rank << _LINE_BITS) | region.body_start))
        hi = int(np.searchsorted(line_keys, (rank << _LINE_BITS) | region.end_line, side="right"))
        return lo, hi

    handlers_by_region: dict[str, list[Handler]] = defaultdict(list)
    readable: set[str] = set()
    for path in prod_paths:
        if not path.endswith(".py") or (source := read(path)) is None:
            continue
        readable.add(path)
        static = data.static.get(path)
        statements = set(static.statements) if static is not None else None
        excluded = set(static.excluded) if static is not None else set()
        enclosing = sorted(regions_by_path.get(path, ()), key=lambda r: (r.def_line, -r.end_line))
        rank = rank_of[path]
        for handler in find_handlers(path, source):
            if handler.body_line in excluded or (
                statements is not None and handler.body_line not in statements
            ):
                continue
            owner = None
            for region in enclosing:
                if region.def_line <= handler.line <= region.end_line:
                    owner = region  # sorted by start: the last match is the innermost
            key = (rank << _LINE_BITS) | handler.body_line
            i = int(np.searchsorted(executed_keys, key))
            executed = i < executed_keys.size and int(executed_keys[i]) == key
            placed = Handler(
                path,
                handler.line,
                handler.body_line,
                handler.types,
                executed,
                owner.func_key if owner else None,
            )
            handlers_by_region[placed.func_key or f"{path}::<module>"].append(placed)
    if any(p.endswith(".py") for p in prod_paths) and not readable:
        notes.append("exception handlers not measured: no production source was available")

    function_rows: list[FunctionTesting] = []
    covered_tests: dict[str, np.ndarray] = {}
    for path, path_regions in sorted(regions_by_path.items()):
        static = data.static.get(path)
        excluded = frozenset(static.excluded) if static is not None else frozenset()
        for region in path_regions:
            if region.def_line in excluded or region.body_start in excluded:
                continue  # ``# pragma: no cover`` and friends: deliberately not tested
            callable_ = region.kind in ("function", "method")
            region_handlers = handlers_by_region.get(region.func_key, [])
            if not callable_ and not region_handlers:
                continue
            covered = False
            if callable_:
                lo, hi = line_range(region)
                covered = hi > lo
                if covered and stale_lookup is not None:
                    covered_tests[region.func_key] = np.unique(
                        by_line_test[line_starts[lo] : line_starts[hi]]
                    )
            d, ind = direct.get(region.func_key, set()), indirect.get(region.func_key, set())
            observed = bool(d or ind)
            unexercised = tuple(h for h in region_handlers if not h.executed)
            function_rows.append(
                FunctionTesting(
                    func_key=region.func_key,
                    path=region.path,
                    qualname=region.qualname,
                    kind=region.kind,
                    covered=covered,
                    observed=observed,
                    direct_tests=len(d),
                    indirect_tests=len(ind - d),
                    indirect_only=observed and not d,
                    untested=callable_ and not covered and not observed,
                    handlers_total=len(region_handlers) if path in readable else None,
                    handlers_unexercised=len(unexercised) if path in readable else None,
                    handlers=unexercised,
                )
            )

    # Stale tests.
    stale: list[bool | None] = [None] * n
    if stale_lookup is not None:
        head_regions = {r.func_key: r for r in regions}
        funcs_of_test: dict[int, set[str]] = defaultdict(set)
        for key, holders in covered_tests.items():
            for i in holders.tolist():
                funcs_of_test[i].add(key)
        by_file: dict[str, set[str]] = defaultdict(set)
        for i, keys in funcs_of_test.items():
            by_file[data.test_files[i]].update(keys)
        requests = {path: [head_regions[k] for k in sorted(keys)] for path, keys in sorted(by_file.items())}
        then_by_file = stale_lookup(requests) if requests else {}
        for i in range(n):
            keys = funcs_of_test.get(i, set())
            then = then_by_file.get(data.test_files[i])
            if not keys:
                stale[i] = False
            elif then is not None:
                stale[i] = all(k in then and then[k] != head_regions[k].body_hash for k in keys)
        if n and all(v is None for v in stale):
            notes.append("stale tests not measured: the test files' history could not be read")
    else:
        notes.append("stale tests not measured: no repository history")

    # Per-test rows.
    rows: list[TestEconomy] = []
    for i, nodeid in enumerate(tests):
        w = weights[nodeid]
        u_lines, u_arcs, u_classes = int(unique[_LINES][i]), int(unique[_ARCS][i]), int(unique[_CLASSES][i])
        unique_facts = LINE_WEIGHT * u_lines + ARC_WEIGHT * u_arcs + CLASS_WEIGHT * u_classes
        total = w.total_statements
        duration = data.durations_ms[i]
        empty = not sig_size[i]
        rows.append(
            TestEconomy(
                nodeid=nodeid,
                path=data.test_files[i],
                family=family_of(nodeid),
                outcome=data.outcomes[i],
                weight=w,
                cases=case_count.get(i, 1),
                lines=int(count[_LINES][i]),
                arcs=int(count[_ARCS][i]),
                classes=int(count[_CLASSES][i]),
                observations=int(count[_OBS][i]),
                unique_lines=u_lines,
                unique_arcs=u_arcs,
                unique_classes=u_classes,
                unique_facts=unique_facts,
                leverage=round(unique_facts / max(1, total), 4) if total is not None else None,
                duration_ms=round(duration, 3) if duration is not None else None,
                cost_per_fact_ms=round(duration / unique_facts, 4)
                if duration is not None and unique_facts
                else None,
                signature=signatures[i],
                group_id=group_of.get(i),
                dominated_by=tests[dominated_by[i]] if i in dominated_by else None,
                in_minimal_set=i in minimal,
                removable=i not in minimal and not empty and data.outcomes[i] != "skipped",
                assertion_free=(
                    (w.asserts or 0) + (w.helper_asserts or 0) == 0 and not w.raises if w.measured else None
                ),
                stale=stale[i],
                empty=empty,
            )
        )

    tax = None
    if base is not None:
        union: dict[str, set[int]] = defaultdict(set)
        for key in line_keys.tolist():
            union[prod_paths[key >> _LINE_BITS]].add(key & ((1 << _LINE_BITS) - 1))
        head_classes = set(class_keys)
        tax = _tax(rows, head_classes, union, read, base, policy, test_files, weights)
    else:
        notes.append("PR test tax not computed: no base run")

    totals = _totals(rows, family_rows, function_rows, len(minimal), kinds, bool(readable))
    return EconomyReport(
        tests=tuple(rows),
        families=tuple(family_rows),
        functions=tuple(function_rows),
        totals=totals,
        tax=tax,
        notes=tuple(notes),
    )


def _tax(
    rows: Sequence[TestEconomy],
    head_classes: set[tuple[str, str]],
    union: Mapping[str, set[int]],
    read: Callable[[str], str | None],
    base: BaseSnapshot,
    policy: EconomyPolicy,
    test_files: set[str],
    weights: Mapping[str, TestWeight],
) -> TestTax:
    head_test_paths = {p for p in test_files if p.endswith(".py")}
    for path in list(head_test_paths):
        head_test_paths.update(c for c in conftest_paths(path) if read(c) is not None)
    for weight in weights.values():
        head_test_paths.update(h.split("::", 1)[0] for h in weight.helpers)
    paths = sorted(head_test_paths | {p for p in base.test_sources if p.endswith(".py")})
    added = removed = changed = 0
    for path in paths:
        delta = statement_delta(base.test_sources.get(path), read(path))
        if delta is None:
            continue
        added += delta[0]
        removed += delta[1]
        changed += 1 if delta != (0, 0) else 0

    newly = 0
    for path, lines in union.items():
        base_cov = base.coverage.get(path, frozenset())
        base_source, head_source = base.sources.get(path), read(path)
        mapping = (
            _line_map(base_source, head_source)
            if base_source is not None and head_source is not None
            else None
        )
        for line in lines:
            old = mapping.get(line) if mapping is not None else line
            if old is None or old not in base_cov:
                newly += 1

    new_classes = len(head_classes - base.classes)
    new_tests = [r for r in rows if r.nodeid not in base.tests]
    tax = added / max(1, newly + CLASS_WEIGHT * new_classes)
    return TestTax(
        base_run_id=base.run_id,
        base_sha=base.sha,
        base_exact=base.exact,
        added_statements=added,
        removed_statements=removed,
        test_files_changed=changed,
        newly_covered_lines=newly,
        new_classes=new_classes,
        new_tests=len(new_tests),
        new_tests_unique_facts=sum(r.unique_facts for r in new_tests),
        tax=round(tax, 3),
        threshold=policy.test_tax_threshold,
        min_statements=policy.test_tax_min_statements,
        flagged=added >= policy.test_tax_min_statements and tax > policy.test_tax_threshold,
        message=tax_message(added, newly, new_classes),
    )


def _totals(
    rows: Sequence[TestEconomy],
    families: Sequence[FamilyEconomy],
    functions: Sequence[FunctionTesting],
    minimal_size: int,
    kinds: Sequence[int],
    handlers_known: bool,
) -> RunTotals:
    measured = [r for r in rows if r.measured]
    removable = [r for r in rows if r.removable]
    grouped = [r for r in rows if r.group_id is not None]
    stale_values = [r.stale for r in rows if r.stale is not None]
    handler_rows = [f for f in functions if f.handlers_total is not None]
    handlers = handlers_known and bool(handler_rows)
    return RunTotals(
        tests=len(rows),
        measured_tests=len(measured),
        statements=sum(r.weight.statements or 0 for r in measured),
        helper_statements=sum(r.weight.helper_statements or 0 for r in measured),
        asserts=sum((r.weight.asserts or 0) + (r.weight.helper_asserts or 0) for r in measured),
        assertion_free=sum(1 for r in rows if r.assertion_free),
        lines=int(kinds[_LINES]),
        arcs=int(kinds[_ARCS]),
        classes=int(kinds[_CLASSES]),
        observations=int(kinds[_OBS]),
        unique_facts=sum(r.unique_facts for r in rows),
        total_duration_ms=round(sum(r.duration_ms or 0.0 for r in rows), 3),
        minimal_set_size=minimal_size,
        minimal_set_duration_ms=round(sum(r.duration_ms or 0.0 for r in rows if r.in_minimal_set), 3),
        removable_candidates=len(removable),
        removable_duration_ms=round(sum(r.duration_ms or 0.0 for r in removable), 3),
        equivalence_groups=len({r.group_id for r in grouped}),
        tests_in_groups=len(grouped),
        dominated=sum(1 for r in rows if r.dominated_by is not None),
        empty_tests=sum(1 for r in rows if r.empty),
        families=len(families),
        collapsible_families=sum(1 for f in families if f.redundant_cases > 0),
        redundant_cases=sum(f.redundant_cases for f in families),
        stale_tests=sum(1 for v in stale_values if v) if stale_values else None,
        functions=sum(1 for f in functions if f.kind in ("function", "method")),
        indirect_only_functions=sum(1 for f in functions if f.indirect_only),
        untested_functions=sum(1 for f in functions if f.untested),
        handlers_total=sum(f.handlers_total or 0 for f in handler_rows) if handlers else None,
        handlers_unexercised=sum(f.handlers_unexercised or 0 for f in handler_rows) if handlers else None,
    )
