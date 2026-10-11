"""The shape-key grammar: parsing, the version-1 vocabulary, canonical order."""

from __future__ import annotations

import pytest

from coverset_model.shape_keys import ShapeKeyError, canonical, is_valid, parse, validate

VALID = [
    "NoneType",
    "bool|true",
    "int|big|neg",
    "int|zero",
    "float|nan",
    "float|inf|inf-neg",
    "float|integral|zero",
    "str|empty|len:0",
    "str|len:1|whitespace",
    "str|ascii|digits|kind:quantity/multi-letter-unit|len:2-10",
    "str|ascii|kind:email/multi-label-domain|len:11-100|ws-edge",
    "str|len:11-100|unicode",
    "str|ascii|kind:iso-date|len:2-10",
    "bytes|len:>1000",
    "list|len:0",
    "list|all_zero|any_zero|dups|elem:int|len:2-10|order:sorted|sum_zero",
    "list|elem:tuple|len:2-10|order:sorted|pair:lt",
    "list|elem:obj:ledger.accounts.roles.Role|len:2-10|order:n/a",
    "tuple|elem:mixed|len:2-10|order:n/a",
    "frozenset|elem:str|len:1",
    "dict|elem:int|key:str|len:1",
    "obj:ledger.billing.invoices.Invoice|dataclass",
    "obj:src/models.User",
]

INVALID = [
    ("", "1 to 512"),
    ("x" * 600, "1 to 512"),
    ("integer|pos", "unknown type token"),
    ("int|pos|neg|zero", "not sorted"),
    ("int|pos|pos", "repeated"),
    ("int|positive", "does not take the feature"),
    ("str|kind:email/bogus|len:2-10", "unknown kind: value"),
    ("str|len:3|ascii", "unknown len: value"),
    ("list|elem:widget|len:1", "type token or mixed"),
    ("bool|len:1", "does not take 'len'"),
    ("obj:|dataclass", "unknown type token"),
    ("int||pos", "empty or blank"),
    ("str|ascii\n", "printable"),
    (42, "a string"),
]


@pytest.mark.parametrize("key", VALID)
def test_valid_keys(key: str) -> None:
    assert validate(key) == [] and is_valid(key)


@pytest.mark.parametrize(("key", "message"), INVALID)
def test_invalid_keys_say_why(key: object, message: str) -> None:
    problems = validate(key)
    assert problems and any(message in p for p in problems), problems


def test_parse() -> None:
    shape = parse("str|ascii|kind:email/multi-label-domain|len:11-100")
    assert shape.type == "str" and shape.flags == {"ascii"} and shape.group == "str"
    assert shape.has("kind", "email/multi-label-domain") and shape.has("len", "11-100")
    money = parse("obj:ledger.util.money.Money|dataclass")
    assert (money.group, money.type_name, money.flags) == ("object", "Money", {"dataclass"})
    assert parse("NoneType").type_name == "none"
    assert parse("list|elem:obj:a.B|len:1").attrs["elem"] == {"obj:a.B"}
    for bad in ("", "int||pos", "|x"):
        with pytest.raises(ShapeKeyError):
            parse(bad)


def test_canonical_sorts_and_dedupes() -> None:
    assert canonical("int|zero|big|zero") == "int|big|zero"
    assert canonical("NoneType") == "NoneType"
