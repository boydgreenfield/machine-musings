"""Golden tests for the AST function index."""

from __future__ import annotations

import textwrap
from types import CodeType

from coverset.analysis.functions import (
    MODULE_QUALNAME,
    index_functions,
    index_functions_detailed,
    own_lines,
)
from coverset.lang.base import func_key

SOURCE = textwrap.dedent(
    '''\
    """Module docstring."""
    import functools
    from typing import overload

    LIMIT = 3


    def deco(fn):
        return fn


    @deco
    @functools.cache
    def decorated(x: int, *rest: str, flag: bool = False, **extra) -> int:
        """Docstring."""
        return x


    class Account:
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
    '''
)


def _code_objects(code: CodeType) -> list[CodeType]:
    found = []
    for const in code.co_consts:
        if isinstance(const, CodeType) and const.co_name != "<lambda>":
            found.append(const)
            found.extend(_code_objects(const))
    return found


def test_regions_follow_co_qualname_and_co_firstlineno() -> None:
    regions = index_functions("pkg/mod.py", SOURCE)
    code = compile(SOURCE, "pkg/mod.py", "exec")
    compiled = sorted((c.co_qualname, c.co_firstlineno) for c in _code_objects(code))
    indexed = sorted((r.qualname, r.def_line) for r in regions if r.kind != "module")
    assert indexed == compiled


def test_decorated_function_region() -> None:
    region = next(r for r in index_functions("pkg/mod.py", SOURCE) if r.qualname == "decorated")
    assert region.kind == "function"
    assert region.def_line == 12  # first decorator line == co_firstlineno
    assert region.body_start == 16  # first statement after the docstring
    assert region.end_line == 16
    assert region.params == ("x", "*rest", "flag", "**extra")
    assert region.ordinal == 0
    assert region.func_key == func_key("pkg/mod.py", "decorated", 0)
    assert len(region.func_key) == 16


def test_property_getter_and_setter_share_qualname_with_ordinals() -> None:
    regions = [r for r in index_functions("pkg/mod.py", SOURCE) if r.qualname == "Account.balance"]
    assert [(r.ordinal, r.kind, r.params) for r in regions] == [
        (0, "method", ("self",)),
        (1, "method", ("self", "value")),
    ]
    assert regions[0].func_key != regions[1].func_key
    code = compile(
        "class A:\n    @property\n    def x(self): ...\n    @x.setter\n    def x(self, v): ...\n", "a", "exec"
    )
    names = [c.co_qualname for c in _code_objects(code)]
    assert names.count("A.x") == 2


def test_nested_classes_and_functions_and_conditional_defs() -> None:
    by_name: dict[str, list] = {}
    for region in index_functions("pkg/mod.py", SOURCE):
        by_name.setdefault(region.qualname, []).append(region)
    assert by_name["Account"][0].kind == "class"
    assert by_name["Account.Inner"][0].kind == "class"
    assert by_name["Account.Inner.method"][0].kind == "method"
    assert [r.ordinal for r in by_name["outer"]] == [0, 1]  # if/else redefinition
    assert by_name["outer.<locals>.inner"][0].kind == "function"
    assert "fallback" in by_name  # defined inside try/except
    assert not any("<lambda>" in name for name in by_name)


def test_overloads_get_ordinals() -> None:
    regions = [r for r in index_functions("pkg/mod.py", SOURCE) if r.qualname == "parse"]
    assert [r.ordinal for r in regions] == [0, 1, 2]
    assert len({r.func_key for r in regions}) == 3
    assert regions[0].sig_hash != regions[1].sig_hash


def test_module_region_and_own_lines() -> None:
    regions = index_functions("pkg/mod.py", SOURCE)
    module = regions[0]
    assert (module.qualname, module.kind, module.def_line) == (MODULE_QUALNAME, "module", 1)
    assert module.body_start == 2  # after the module docstring
    assert module.end_line == len(SOURCE.splitlines())
    module_lines = own_lines(module, regions)
    assert 5 in module_lines  # LIMIT = 3
    assert 16 not in module_lines  # inside decorated()
    account = next(r for r in regions if r.qualname == "Account")
    class_lines = own_lines(account, regions)
    assert 22 in class_lines  # rate = 2
    assert 26 not in class_lines  # inside the getter


def test_hashes_ignore_formatting_comments_and_docstrings() -> None:
    a = "def f(x):\n    '''doc'''\n    return x + 1  # note\n"
    b = "def f(x):\n\n    return (x +\n            1)\n"
    c = "def f(x):\n    return x + 2\n"
    d = "@decorator\ndef f(x):\n    return x + 1\n"
    (ra,), (rb,), (rc,), (rd,) = (
        [r for r in index_functions("m.py", s) if r.kind == "function"] for s in (a, b, c, d)
    )
    assert ra.body_hash == rb.body_hash and ra.sig_hash == rb.sig_hash
    assert ra.body_hash != rc.body_hash
    assert ra.body_hash == rd.body_hash and ra.sig_hash != rd.sig_hash


def test_class_hash_ignores_method_bodies() -> None:
    a = "class C:\n    x = 1\n    def m(self):\n        return 1\n"
    b = "class C:\n    x = 1\n    def m(self):\n        return 2\n"
    ca, cb = (next(r for r in index_functions("m.py", s) if r.kind == "class") for s in (a, b))
    assert ca.body_hash == cb.body_hash
    ma, mb = (next(r for r in index_functions("m.py", s) if r.kind == "method") for s in (a, b))
    assert ma.body_hash != mb.body_hash


def test_annotations_are_attached() -> None:
    detailed = {d.region.qualname: d for d in index_functions_detailed("pkg/mod.py", SOURCE)}
    entry = detailed["decorated"]
    assert entry.annotations == ("int", "str", "bool", None)
    assert entry.returns == "int"


def test_syntax_error_yields_a_module_region_only() -> None:
    regions = index_functions("bad.py", "def broken(:\n    pass\n")
    assert [(r.kind, r.qualname) for r in regions] == [("module", MODULE_QUALNAME)]
    other = index_functions("bad.py", "def broken(:\n    pass  \n")
    assert regions[0].body_hash != other[0].body_hash


def test_docstring_only_body_starts_at_the_line_a_call_records() -> None:
    source = textwrap.dedent(
        '''\
        import functools


        @functools.cache
        def stub():
            """Only a docstring: a call records co_firstlineno and nothing else."""


        def one_liner(x): return x


        def documented():
            """Doc."""
            return 1
        '''
    )
    regions = {r.qualname: r for r in index_functions("m.py", source)}
    assert (regions["stub"].def_line, regions["stub"].body_start) == (4, 4)
    assert regions["one_liner"].body_start == regions["one_liner"].def_line == 9
    assert regions["documented"].body_start == 14
