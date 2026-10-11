"""Regression tests for the correctness review: overloaded targets, the body-line rule for invariant
lenses, and argv-safe ``[tool.coverset]`` values."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coverset.analysis.functions import index_functions, is_stub
from coverset.analysis.invariants import Invariant, Scope, VerifySpec, resolve_target
from coverset.lang.base import StaticFile
from coverset.lang.python import ConfigError, read_tool_config
from coverset.orchestrator import verdict as verdict_module

OVERLOADED = '''from typing import overload


@overload
def f(x: int) -> int: ...
@overload
def f(x: str) -> str: ...
def f(x):
    if isinstance(x, int):
        return x + 1
    return x + "!"


class P:
    def stub(self):
        """Only a docstring."""

    def passes(self):
        pass
'''


def _inv(target: str) -> Invariant:
    return Invariant(
        id="inv",
        scope=Scope("function", target),
        statement="s",
        severity="error",
        verify=(VerifySpec("test"),),
    )


def test_an_overloaded_target_is_its_implementation() -> None:
    regions = index_functions("src/pkg/m.py", OVERLOADED)
    hit = resolve_target(_inv("pkg.m.f"), regions, "src")
    assert hit is not None and (hit.ordinal, hit.def_line, hit.body_start, hit.end_line) == (2, 8, 9, 11)
    stubs = {r.qualname: is_stub(r) for r in regions if r.kind in ("function", "method")}
    assert stubs == {"f": False, "P.stub": True, "P.passes": True}
    assert [is_stub(r) for r in regions if r.qualname == "f"] == [True, True, False]


def test_a_target_with_only_stubs_resolves_to_the_first() -> None:
    regions = index_functions("src/pkg/m.py", "def g(): ...\ndef g(): ...\n")
    hit = resolve_target(_inv("pkg.m.g"), regions, "src")
    assert hit is not None and hit.ordinal == 0


# -- C6: changed lines of an invariant target follow the body-line rule ---------------------------------

TARGET = '''import functools


@functools.cache
def target(a: int,
           b: int) -> int:
    """Doc."""
    total = a + b
    # a comment
    return total
'''


async def _asked(
    changed: set[int], monkeypatch: pytest.MonkeyPatch, statics: Any = None
) -> list[tuple[int, int]]:
    region = next(r for r in index_functions("src/p.py", TARGET) if r.qualname == "target")
    plan = SimpleNamespace(
        changes=[SimpleNamespace(region=SimpleNamespace(func_key=region.func_key))],
        changed_lines={"src/p.py": changed},
    )
    asked: list[tuple[int, int]] = []

    def covering(store: Any, repo_id: int, run_id: str, path: str, start: int, end: int) -> list[str]:
        asked.append((start, end))
        return []

    monkeypatch.setattr(verdict_module, "tests_covering", covering)
    run = SimpleNamespace(repo_id=1, id="r")
    await verdict_module._covers_changed_lines(object(), run, plan, region, ["t"], statics=statics)  # type: ignore[arg-type]
    return asked


async def test_signature_and_decorator_changes_ask_about_the_body(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _asked({4}, monkeypatch) == [(8, 10)]  # decorator only
    assert await _asked({5, 6}, monkeypatch) == [(8, 10)]  # signature only
    assert await _asked({7}, monkeypatch) == [(8, 10)]  # docstring only
    assert await _asked({6, 8}, monkeypatch) == [(8, 8)]  # a body line changed too: just that line


async def test_non_statement_lines_are_left_out_with_static_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    static = {"src/p.py": StaticFile("src/p.py", 10, (1, 4, 5, 8, 10), (), ())}
    assert await _asked({9}, monkeypatch, static) == [(8, 10)]  # only the comment: the whole body
    assert await _asked({9, 10}, monkeypatch, static) == [(10, 10)]


# -- C20: [tool.coverset] values that end up in pytest's argv ------------------------------------------------


@pytest.mark.security
@pytest.mark.parametrize("value", ["-p evil", "--rootdir=/", "@args.txt", "-x"])
def test_testpaths_cannot_be_options(value: str) -> None:
    with pytest.raises(ConfigError, match="must not start"):
        read_tool_config(f'[tool.coverset]\ntestpaths = ["{value}"]\n')
    with pytest.raises(ConfigError, match="must not start"):
        read_tool_config(f'[tool.pytest.ini_options]\ntestpaths = ["{value}"]\n')


@pytest.mark.security
@pytest.mark.parametrize("key", ["source", "trace_modules"])
@pytest.mark.parametrize("value", ["-p", "--evil", "@file"])
def test_module_lists_cannot_be_options(key: str, value: str) -> None:
    with pytest.raises(ConfigError, match="dotted module name"):
        read_tool_config(f'[tool.coverset]\n{key} = ["{value}"]\n')


@pytest.mark.parametrize(("value", "python"), [("03.13", "3.13"), ("3.013", "3.13"), ("3.12.7", "3.12")])
def test_python_versions_are_normalised(value: str, python: str) -> None:
    assert read_tool_config(f'[tool.coverset]\npython = "{value}"\n').python == python


@pytest.mark.parametrize("value", ["3.١٣", "3." + "1" * 50])
def test_python_versions_are_ascii_and_short(value: str) -> None:
    with pytest.raises(ConfigError):
        read_tool_config(f'[tool.coverset]\npython = "{value}"\n')
