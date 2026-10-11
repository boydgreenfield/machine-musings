"""pytest-coverset's function regions equal the control plane's, field by field.

``pytest_coverset.regions`` reproduces ``coverset.analysis.functions.index_functions`` without
depending on coverset; these tests pin the two together on real sources (the plugin's fixture
project and the demo app) and on a source built to hit every rule.
"""

from __future__ import annotations

import dataclasses
import textwrap
from pathlib import Path

import pytest
from conformance_helpers import DEMO_SRC, FIXTURE_PROJECT, ROOT

from coverset.analysis.functions import index_functions as control_plane_index
from pytest_coverset.regions import index_functions as plugin_index

SOURCES = sorted(
    p for base in (FIXTURE_PROJECT, DEMO_SRC) for p in base.rglob("*.py") if "__pycache__" not in p.parts
)

TRICKY = textwrap.dedent(
    '''\
    """Module docstring."""
    import functools
    from typing import overload

    LIMIT = 3


    def deco(fn):
        return fn


    @deco
    @functools.cache
    def decorated[T](x: T, /, *rest: str, flag: bool = False, **extra) -> T:
        """Docstring."""
        return x


    class Account[K](Base, metaclass=Meta):
        """An account."""

        rate = 2

        @property
        def balance(self):
            return 1

        @balance.setter
        def balance(self, value):
            pass

        class Inner:
            def method(self):
                return 2

        if rate:
            def conditional(self):
                ...


    if LIMIT:
        def outer():
            def inner():
                return lambda: 1
            return inner
    else:
        def outer():
            return None


    @overload
    def parse(x: int) -> int: ...
    @overload
    def parse(x: str) -> str: ...
    def parse(x):
        return x


    try:
        import json
    except ImportError:
        def fallback():
            pass
    finally:
        class Late:
            """Only a docstring."""


    match LIMIT:
        case 3:
            async def matched(a, *, b):
                async with a:
                    return [c for c in b]
        case _:
            pass


    with open(__file__) as fh:
        def in_with():
            """Doc."""


    def docstring_only():
        """Nothing else."""


    def one_line(): return 1


    for _ in range(1):
        def in_loop(): pass
    '''
)


def assert_same(path: str, source: str) -> None:
    ours = [dataclasses.astuple(region) for region in plugin_index(path, source)]
    theirs = [
        (
            r.func_key,
            r.path,
            r.qualname,
            r.ordinal,
            r.def_line,
            r.body_start,
            r.end_line,
            r.body_hash,
            r.sig_hash,
            r.params,
            r.kind,
        )
        for r in control_plane_index(path, source)
    ]
    assert ours == theirs


@pytest.mark.parametrize("source_file", SOURCES, ids=lambda p: str(p.relative_to(ROOT)))
def test_regions_match_the_control_plane(source_file: Path) -> None:
    rel = source_file.relative_to(ROOT).as_posix()
    assert_same(rel, source_file.read_text(encoding="utf-8"))


def test_sources_were_found() -> None:
    assert len([p for p in SOURCES if p.is_relative_to(DEMO_SRC)]) >= 5
    assert len([p for p in SOURCES if p.is_relative_to(FIXTURE_PROJECT)]) >= 5


@pytest.mark.parametrize(
    "source",
    [
        TRICKY,
        "",
        "\n\n",
        '"""Only a docstring."""\n',
        "def broken(:\n    pass\n",
        "x = 1\r\ny = 2\r\n",
        "class C:\n    pass\nclass C:\n    def f(self): ...\n",
        "def f():\n    def f():\n        def f(): pass\n",
        "x = 1\x0cy = 2\n",
    ],
    ids=[
        "tricky",
        "empty",
        "blank",
        "docstring",
        "syntax-error",
        "crlf",
        "redefined",
        "nested-same",
        "formfeed",
    ],
)
def test_regions_match_on_edge_cases(source: str) -> None:
    assert_same("pkg/edge.py", source)
