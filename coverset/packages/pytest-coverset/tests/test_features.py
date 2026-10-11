"""Shape keys, string kinds and example redaction (pure functions, no tracer)."""

from __future__ import annotations

import pytest

from pytest_coverset.features import len_bucket, shape_key, string_kind
from pytest_coverset.redact import example_repr, scrub


@pytest.mark.parametrize(
    ("value", "key"),
    [
        (None, "NoneType"),
        (True, "bool|true"),
        (False, "bool|false"),
        (0, "int|zero"),
        (-3, "int|neg"),
        (2**40, "int|big|pos"),
        (float("nan"), "float|nan"),
        (float("inf"), "float|inf"),
        (float("-inf"), "float|inf|inf-neg"),
        (-0.0, "float|integral|zero"),
        (2.5, "float|pos"),
        ("", "str|empty|len:0"),
        ("   ", "str|len:2-10|whitespace"),
        (" x1", "str|ascii|digits|len:2-10|ws-edge"),
        ("héllo", "str|len:2-10|unicode"),
        (b"abc", "bytes|len:2-10"),
        ([], "list|len:0"),
        ([3, -1, 2], "list|any_neg|elem:int|len:2-10|order:unsorted"),
        ([0.0, 0.0], "list|all_zero|any_zero|dups|elem:float|len:2-10|order:sorted|sum_zero"),
        ([3, 2, 1], "list|elem:int|len:2-10|order:reverse"),
        ([1, "a"], "list|elem:mixed|len:2-10|order:n/a"),
        ([1.0, float("nan")], "list|any_nan|elem:float|len:2-10|order:n/a"),
        ([(1, 3), (8, 10), (2, 6)], "list|elem:tuple|len:2-10|order:unsorted|pair:lt"),
        ([(5, 1)], "list|elem:tuple|len:1|pair:gt"),
        ([(1, 2), (2, 2)], "list|elem:tuple|len:2-10|order:sorted|pair:mixed"),
        ([[1, 1], [2, 2]], "list|elem:list|len:2-10|order:sorted|pair:eq"),
        ((1,), "tuple|elem:int|len:1"),
        ({"a": 1}, "dict|elem:int|key:str|len:1"),
        ({1: "a", "b": 2}, "dict|elem:mixed|key:mixed|len:2-10"),
        ({1, 2}, "set|elem:int|len:2-10"),
        (frozenset(), "frozenset|len:0"),
    ],
)
def test_shape_keys(value: object, key: str) -> None:
    assert shape_key(value) == key


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("alice@example.com", "email/plain"),
        ("bob@eng.acme.com", "email/multi-label-domain"),
        ("carol@example.co.uk", "email/plain"),
        ("dave@eng.example.co.uk", "email/multi-label-domain"),
        ("d+tag@x.io", "email/plus-tag"),
        ("Eve@Example.com", "email/uppercase"),
        ("josé@example.com", "email/idn"),
        ("x@xn--bcher-kva.example", "email/idn"),
        ("1h30m", "quantity/multi-unit"),
        ("100ms", "quantity/multi-letter-unit"),
        ("1.5h", "quantity/decimal"),
        ("-5m", "quantity/signed"),
        ("1hx", "quantity/trailing-garbage"),
        ("5m", "quantity/single-unit"),
        ("10 apples", "quantity/multi-letter-unit"),
        ("3 h!", "quantity/trailing-garbage"),
        ("42", "numeric"),
        ("-1.5e3", "numeric"),
        ("2026-10-11", "iso-date"),
        ("2026-10-11T08:00:00Z", "iso-date"),
        ("https://example.com/a?b=1", "url"),
        ("hello world", None),
    ],
)
def test_string_kinds(text: str, kind: str | None) -> None:
    assert string_kind(text) == kind


def test_len_buckets() -> None:
    assert [len_bucket(n) for n in (0, 1, 2, 10, 11, 100, 101, 1000, 1001)] == [
        "0",
        "1",
        "2-10",
        "2-10",
        "11-100",
        "11-100",
        "101-1000",
        "101-1000",
        ">1000",
    ]


class Explosive:
    """Every dunder a careless tracer might call raises."""

    def __len__(self) -> int:
        raise AssertionError("__len__ called")

    def __iter__(self):
        raise AssertionError("__iter__ called")

    def __bool__(self) -> bool:
        raise AssertionError("__bool__ called")

    def __repr__(self) -> str:
        raise AssertionError("__repr__ called")

    def __eq__(self, other: object) -> bool:
        raise AssertionError("__eq__ called")

    def __lt__(self, other: object) -> bool:
        raise AssertionError("__lt__ called")

    def __hash__(self) -> int:
        return 1


class ExplosiveList(list):
    def __len__(self) -> int:
        raise AssertionError("__len__ called")


def test_user_dunders_are_never_called() -> None:
    boom = Explosive()
    assert shape_key(boom) == f"obj:{__name__}.Explosive"
    assert shape_key([boom, boom]) == f"list|elem:obj:{__name__}.Explosive|len:2-10|order:n/a"
    assert shape_key({boom: boom}) == f"dict|elem:obj:{__name__}.Explosive|key:obj:{__name__}.Explosive|len:1"
    assert shape_key(ExplosiveList([1])) == f"obj:{__name__}.ExplosiveList"
    assert (
        example_repr("x", [boom, {boom: 1}], "all")
        == f"[<{__name__}.Explosive>, {{<{__name__}.Explosive>: 1}}]"
    )
    assert example_repr("x", boom, "all") is None


def test_example_redaction() -> None:
    assert example_repr("password", "hunter2hunter2", "scalars") == "<redacted:len=14>"
    assert example_repr("api_key", 12345, "scalars") == "<redacted>"
    assert example_repr("email", "bob@sub.example.com", "scalars") == "'b***@sub.example.com'"
    assert example_repr("note", "token ghp_" + "a" * 36, "scalars") == "'token ghp_[REDACTED]'"
    assert example_repr("values", [1, 2], "scalars") is None
    assert example_repr("values", [1, 2], "all") == "[1, 2]"
    assert example_repr("n", 3, "off") is None
    assert len(example_repr("s", "x" * 500, "scalars") or "") <= 50
    assert scrub("Authorization: Bearer abc.def") == "Authorization: Bearer [REDACTED]"


def test_return_values_of_secret_named_functions_are_redacted() -> None:
    secret = "ghx_supersecretvalue1234567890"
    assert example_repr("return", secret, "scalars", func_name="make_reset_token") == "<redacted:len=30>"
    assert example_repr("return", 7, "scalars", func_name="get_api_key") == "<redacted>"
    assert example_repr("return", "abc", "scalars", func_name="normalise") == "'abc'"
    assert example_repr("text", "abc", "scalars", func_name="make_token") == "'abc'"


def test_secret_dict_keys_are_redacted_in_container_examples() -> None:
    kwargs = {"password": "hunter2", "api_key": "abc", "name": "bob", "nested": {"session_id": [1, 2]}}
    assert example_repr("**kw", kwargs, "all") == (
        "{'password': <redacted:len=7>, 'api_key': <redacted:len=3>, 'name': 'bob', "
        "'nested': {'session_id': <redacted:len=2>}}"
    )
    assert example_repr("**kw", {"token": 5}, "all") == "{'token': <redacted>}"


def test_huge_ints_and_long_bytes_are_summarised() -> None:
    assert example_repr("n", 10**5000, "scalars") == "<int 16610 bits>"
    assert example_repr("n", -(2**200), "scalars") == "<int 201 bits>"
    assert example_repr("n", 2**100, "scalars") == str(2**100)
    assert example_repr("b", b"x" * 1000, "scalars") == "b'" + "x" * 22 + "'...b'" + "x" * 23 + "'"
    assert example_repr("b", b"\xff" * 3, "scalars") == "b'\\xff\\xff\\xff'"
