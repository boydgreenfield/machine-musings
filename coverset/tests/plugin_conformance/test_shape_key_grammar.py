"""Every shape key pytest-coverset emits is in the neutral grammar (``coverset_model.shape_keys``).

The corpus: the values parametrised in the plugin's ``tests/test_features.py`` (shape keys and
string kinds), a list of awkward values computed here, and the keys a real run of the plugin
records over hostile arguments (huge ints, NaN and infinities in containers, odd receivers,
classes defined in functions, non-ASCII names, dataclasses, secrets).
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
import importlib.util
import json
import re
from collections import OrderedDict
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from conformance_helpers import PLUGIN_TESTS, run_pytest

shape_keys = pytest.importorskip("coverset_model.shape_keys")

from pytest_coverset.features import shape_key  # noqa: E402


def _load_test_features() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "_coverset_plugin_test_features", PLUGIN_TESTS / "test_features.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _parametrized(func: Any) -> list[tuple[Any, ...]]:
    [mark] = [m for m in func.pytestmark if m.name == "parametrize"]
    return list(mark.args[1])


TEST_FEATURES = _load_test_features()
FEATURE_VALUES = [value for value, _ in _parametrized(TEST_FEATURES.test_shape_keys)]
FEATURE_KEYS = [key for _, key in _parametrized(TEST_FEATURES.test_shape_keys)]
STRING_KIND_TEXTS = [text for text, _ in _parametrized(TEST_FEATURES.test_string_kinds)]


@dataclasses.dataclass
class Point:
    x: int


class Ünïcode:
    pass


def _local_class() -> type:
    class Local:
        pass

    return Local


EXTRA_VALUES: list[Any] = [
    -(2**200),
    2**31,
    -(2**31) - 1,
    1e308 * 10,
    -1e-320,
    0.5,
    "x" * 5000,
    " \t ",
    "١٢٣",
    "2026-10-11",
    "https://example.com/a?b=c",
    "12.5 kg",
    "-3 m/s",
    "5 kgx!",
    "a.b+c@Example.COM",
    b"",
    b"\x00" * 2000,
    bytearray(b"ab"),
    [float("inf"), float("-inf")],
    [float("nan")] * 3,
    [0, 0, 0],
    [1, -1],
    [[1, 2], [3]],
    [(1, 2, 3)],
    [(2, 1), (3, 1)],
    list(range(200)),
    list(range(2000, 0, -1)),
    tuple("abc"),
    {(): None},
    {None: 1, 2: None},
    {frozenset({1}): "a"},
    OrderedDict(a=1),
    set(range(50)),
    frozenset({"a", 1}),
    Point(1),
    Ünïcode(),
    _local_class()(),
    Decimal("1.5"),
    object(),
    type,
    print,
    lambda: None,
    range(3),
    ...,
    NotImplemented,
    1j,
]

HOSTILE_MODULE = """
import dataclasses


class Sneaky:
    @property
    def __class__(self):
        raise RuntimeError("never")


class Ünïcode:
    def act(self, x):
        return x


@dataclasses.dataclass
class Point:
    x: int


def anything(*args, **kwargs):
    return args


def one(value, password=None):
    return value


def make():
    class Local:
        pass
    return Local()
"""

HOSTILE_TESTS = """
from hostile.mod import Point, Sneaky, Ünïcode, anything, make, one

VALUES = [
    10**5000, -(10**40), float("nan"), float("-inf"), -0.0, [float("inf")], {"k": [1, "a"]},
    b"\\x00" * 3000, "josé@example.com", "  padded ", "12.5 kg", "2026-01-02", "x" * 10000,
    "", "\\u00a0", "١٢٣", Sneaky(), Point(1), make(), [Point(2), None], {1: 2, "a": 3},
    frozenset(), {1, 2}, [(1, 2), (2, 1)], [[0, 0], [0, 0]], (), {}, [], [3, 2, 1], True, None,
]


def test_hostile():
    for value in VALUES:
        one(value, password="hunter2")
    anything(*VALUES, password="hunter2")
    Ünïcode().act(1.0)
    Ünïcode.act(Sneaky(), -0.0)
"""


def assert_in_grammar(key: str) -> None:
    shape_keys.parse(key)  # raises on a malformed key
    assert shape_keys.validate(key) == [], key


@pytest.mark.parametrize("key", FEATURE_KEYS)
def test_test_features_keys_are_in_the_grammar(key: str) -> None:
    assert_in_grammar(key)


def _value_id(value: Any) -> str:
    """An id that is the same in every process: pytest-xdist workers refuse to run when their ids
    differ ("Different tests were collected"). An object's repr holds its memory address and a set's
    order follows string hashing, which is randomised per process, so those are spelled stably. A
    long one keeps its first 60 characters and a digest of the rest: the 5,000 to 11,000 character
    ids of the huge values were dropped from the neutral bundle (test ids are capped at 4,096)."""
    text = _stable_repr(value)
    if len(text) <= 80:
        return text
    return f"{text[:60]}...{hashlib.sha256(text.encode()).hexdigest()[:12]}"


def _stable_repr(value: Any) -> str:
    if type(value) in (set, frozenset):
        return f"{type(value).__name__}({sorted(_stable_repr(v) for v in value)})"
    return re.sub(r" at 0x[0-9a-f]+>", ">", repr(value))


@pytest.mark.parametrize("value", FEATURE_VALUES + STRING_KIND_TEXTS + EXTRA_VALUES, ids=_value_id)
def test_computed_keys_are_in_the_grammar(value: Any) -> None:
    assert_in_grammar(shape_key(value))


def test_keys_recorded_over_hostile_values_are_in_the_grammar(tmp_path: Path) -> None:
    (tmp_path / "hostile").mkdir()
    (tmp_path / "hostile" / "__init__.py").write_text("")
    (tmp_path / "hostile" / "mod.py").write_text(HOSTILE_MODULE, encoding="utf-8")
    (tmp_path / "test_hostile.py").write_text(HOSTILE_TESTS, encoding="utf-8")
    proc = run_pytest(
        tmp_path,
        "--coverset-out=out",
        "--coverset-source=hostile",
        "--coverset-repr-examples=all",
        "--coverset-neutral",
    )
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    with gzip.open(tmp_path / "out" / "shapes.jsonl.gz", "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())
    keys = header["shapes"]
    assert len(keys) >= 20
    for key in keys:
        assert_in_grammar(key)
    assert any(key.startswith("obj:hostile.mod.make.<locals>.Local") for key in keys)

    from coverset_model import io as model_io

    assert model_io.validate(tmp_path / "out" / "neutral") == []
    bundle = model_io.read_bundle(tmp_path / "out" / "neutral")
    assert {obs.shape_key for obs in bundle.shapes} <= set(keys)
    assert "hunter2" not in (tmp_path / "out" / "neutral.tar.gz").read_bytes().decode("latin-1")
