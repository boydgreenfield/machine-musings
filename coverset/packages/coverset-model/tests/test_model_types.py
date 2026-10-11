"""Type descriptors, Python annotations → descriptors, and the classes a descriptor expects.

``GOLDEN`` was computed by the annotation-text catalogue that preceded descriptors (coverset's
``analysis/shapes.py`` before the model package): the descriptor path must expect exactly the same
classes, in the same order, for the same annotation and observations.
"""

from __future__ import annotations

import pytest

from coverset_model.catalogue import Catalogue, ParamInfo
from coverset_model.neutral import ShapeObs
from coverset_model.python_types import descriptor
from coverset_model.types import (
    ANY,
    DescriptorError,
    expected_families,
    from_json,
    label,
    to_json,
)
from coverset_model.types import (
    TypeDescriptor as T,
)

INT, FLOAT, STR, NONE = T.scalar("int"), T.scalar("float"), T.scalar("str"), T.scalar("none")
ENUMS = {"Color": ("RED", "GREEN")}

GOLDEN = [
    ("int", ["int|pos"], None, ["negative", "zero", "positive"]),
    ("float", ["int|pos"], None, ["negative", "zero", "positive", "nan", "inf"]),
    ("bool", [], None, ["true", "false"]),
    ("int | str", ["int|pos"], None, ["int", "str", "negative", "zero", "positive"]),
    ("Optional[str]", [], None, ["str", "none"]),
    (
        "str | None",
        ["str|ascii|len:2-10"],
        None,
        ["str", "none", "empty", "whitespace", "ascii", "unicode", "ws-edge"],
    ),
    ("Literal['fast', 'slow']", [], None, ["'fast'", "'slow'"]),
    ("Literal[1, 2] | None", ["int|pos"], None, ["none", "negative", "zero", "positive", "1", "2"]),
    (
        "list[float]",
        ["list|elem:int|len:2-10|order:sorted"],
        None,
        [
            "empty",
            "single",
            "multiple",
            "zero-element",
            "negative-element",
            "all-zero",
            "sum-zero",
            "nan-element",
            "unsorted",
            "duplicates",
        ],
    ),
    (
        "list[int | float]",
        ["list|len:0"],
        None,
        [
            "empty",
            "single",
            "multiple",
            "zero-element",
            "negative-element",
            "all-zero",
            "sum-zero",
            "nan-element",
            "unsorted",
            "duplicates",
        ],
    ),
    (
        "list[tuple[int, int]]",
        ["list|len:1"],
        None,
        ["empty", "single", "multiple", "unsorted", "duplicates", "inverted-pair"],
    ),
    (
        "tuple[int, ...]",
        ["tuple|len:0"],
        None,
        [
            "empty",
            "single",
            "multiple",
            "zero-element",
            "negative-element",
            "all-zero",
            "sum-zero",
            "unsorted",
            "duplicates",
        ],
    ),
    (
        "tuple[int, int]",
        ["tuple|elem:int|len:2-10|order:sorted"],
        None,
        [
            "empty",
            "single",
            "multiple",
            "zero-element",
            "negative-element",
            "all-zero",
            "sum-zero",
            "unsorted",
            "duplicates",
        ],
    ),
    (
        "set[int]",
        ["set|len:0"],
        None,
        ["empty", "single", "multiple", "zero-element", "negative-element", "all-zero", "sum-zero"],
    ),
    (
        "frozenset[str] | None",
        ["frozenset|len:0"],
        None,
        ["frozenset", "none", "empty", "single", "multiple"],
    ),
    ("list[str]", ["list|len:0"], None, ["empty", "single", "multiple", "unsorted", "duplicates"]),
    ("list", ["list|len:0"], None, ["empty", "single", "multiple"]),
    ("dict[str, int]", ["dict|len:0"], None, ["empty", "single", "multiple"]),
    ("Color", ["obj:pkg.Color"], "<Color.RED: 1>", ["Color.RED", "Color.GREEN"]),
    ("Color | None", ["obj:pkg.Color"], "<Color.GREEN: 2>", ["Color", "none", "Color.RED", "Color.GREEN"]),
    ("Color", ["obj:pkg.Color"], None, []),
    ("Money | None", [], None, ["Money", "none"]),
    ("Annotated[int, 'x']", ["int|zero"], None, ["negative", "zero", "positive"]),
    (
        "'list[int]'",
        ["list|len:1"],
        None,
        [
            "empty",
            "single",
            "multiple",
            "zero-element",
            "negative-element",
            "all-zero",
            "sum-zero",
            "unsorted",
            "duplicates",
        ],
    ),
    ("Any", ["int|pos"], None, ["negative", "zero", "positive"]),
    ("int | Any", [], None, ["int", "Any"]),
    (
        "Optional[Literal['x', 'y']]",
        ["str|ascii|len:1"],
        "'x'",
        ["none", "empty", "whitespace", "ascii", "unicode", "ws-edge", "'x'", "'y'"],
    ),
    ("list[Optional[float]]", ["list|len:0"], None, ["empty", "single", "multiple"]),
    ("Union[int, Union[str, None]]", [], None, ["int", "str", "none"]),
    ("list[bool]", ["list|len:0"], None, ["empty", "single", "multiple"]),
    (
        "Iterable[float]",
        ["list|len:0"],
        None,
        [
            "empty",
            "single",
            "multiple",
            "zero-element",
            "negative-element",
            "all-zero",
            "sum-zero",
            "nan-element",
            "unsorted",
            "duplicates",
        ],
    ),
    ("Literal[True]", ["bool|true"], "True", ["true", "false", "True"]),
    ("Literal[None]", ["NoneType"], None, ["none", "None"]),
    ("!!bad", ["int|pos"], None, ["negative", "zero", "positive"]),
    (
        None,
        ["int|pos", "str|empty|len:0", "list|len:0"],
        None,
        [
            "negative",
            "zero",
            "positive",
            "str-empty",
            "whitespace",
            "ascii",
            "unicode",
            "ws-edge",
            "container-empty",
            "single",
            "multiple",
        ],
    ),
]


def _obs(key: str, example: str | None) -> ShapeObs:
    return ShapeObs(("p.py", "f", 1), "x", key, "t", 1, ("t",), False, example, "t")


@pytest.mark.parametrize(("annotation", "keys", "example", "expected"), GOLDEN)
def test_descriptors_expect_what_annotation_text_expected(
    annotation: str | None, keys: list[str], example: str | None, expected: list[str]
) -> None:
    observations = [_obs(k, example) for k in keys]
    declared = descriptor(annotation, ENUMS)
    names = [
        c.name.split(":", 1)[1] for c in Catalogue().expected_classes(ParamInfo("x", declared), observations)
    ]
    assert names == expected
    # Text goes through the same conversion when handed to the catalogue directly.
    by_text = Catalogue(ENUMS.get).expected_classes(ParamInfo("x", annotation), observations)
    assert [c.name.split(":", 1)[1] for c in by_text] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("int", INT),
        ("int | None", T.union(INT, NONE)),
        ("Optional[int]", T.union(INT, NONE)),
        ("Union[int, Union[str, None]]", T.union(INT, STR, NONE)),
        ("Literal['a', 'b']", T.literal("a", "b")),
        ("list[float]", T.list_of(FLOAT)),
        ("Sequence[int]", T.list_of(INT)),
        ("tuple[int, ...]", T.tuple_of(elem=INT)),
        ("tuple[int, str]", T.tuple_of([INT, STR])),
        ("tuple", T.tuple_of()),
        ("dict[str, int]", T.dict_of(STR, INT)),
        ("Mapping[str, float]", T.dict_of(STR, FLOAT)),
        ("set[str]", T.set_of(STR)),
        ("FrozenSet[int]", T.set_of(INT, frozen=True)),
        ("Iterable[int]", T.set_of(INT)),
        ("list", T.list_of()),
        ("Color", T.enum("Color", ("RED", "GREEN"))),
        ("models.Color", T.enum("Color", ("RED", "GREEN"))),
        ("Money", T.object("Money")),
        ("Any", ANY),
        ("Annotated[float, 'unit']", FLOAT),
        ("'list[int]'", T.list_of(INT)),
        ("list['float']", T.list_of(FLOAT)),
        ("Literal[Color.RED]", T.object("Literal")),
        ("!!not python", ANY),
        ("x" * 5000, ANY),
    ],
)
def test_python_annotations_become_descriptors(text: str, expected: T) -> None:
    assert descriptor(text, ENUMS) == expected


def test_no_annotation_is_no_descriptor() -> None:
    assert descriptor(None) is None
    assert descriptor("Color") == T.object("Color")  # no enum resolver: just a class name
    assert descriptor("Color", lambda name: ("A",) if name == "Color" else None) == T.enum("Color", ("A",))


def test_json_round_trip_of_every_kind() -> None:
    every = T.union(
        INT, FLOAT, STR, T.scalar("bool"), T.scalar("bytes"), NONE, ANY,
        T.list_of(INT), T.list_of(), T.set_of(STR), T.set_of(STR, frozen=True),
        T.tuple_of([INT, STR]), T.tuple_of(elem=FLOAT), T.tuple_of(), T.dict_of(STR, T.list_of(FLOAT)),
        T.literal("a", 1, 2.5, True, None), T.enum("pkg.Role", ("admin", "member")), T.object("ledger.Money"),
    )  # fmt: skip
    assert from_json(to_json(every)) == every
    assert every.to_json()["kind"] == "union"
    assert to_json(T.list_of()) == {"kind": "list"}
    assert to_json(T.tuple_of([INT])) == {"kind": "tuple", "elems": [{"kind": "int"}]}


@pytest.mark.parametrize(
    "bad",
    [
        "int",
        {"kind": "integer"},
        {"kind": "int", "extra": 1},
        {"kind": "union", "arms": []},
        {"kind": "union"},
        {"kind": "literal", "values": [{"nested": 1}]},
        {"kind": "enum", "name": "", "members": []},
        {"kind": "enum", "name": "Role", "members": [1]},
        {"kind": "object"},
        {"kind": "tuple", "elems": [{"kind": "int"}], "elem": {"kind": "int"}},
        {"kind": "list", "elem": "int"},
        {"kind": "union", "arms": [{"kind": "int"}] * 300},
    ],
)
def test_from_json_refuses_what_is_not_a_descriptor(bad: object) -> None:
    with pytest.raises(DescriptorError):
        from_json(bad)


def test_from_json_bounds_nesting() -> None:
    deep: dict = {"kind": "int"}
    for _ in range(100):
        deep = {"kind": "list", "elem": deep}
    with pytest.raises(DescriptorError, match="deeper"):
        from_json(deep)


def test_labels_and_expectations() -> None:
    assert label(T.object("ledger.util.money.Money")) == "Money"
    assert label(T.enum("pkg.Role", ("a",))) == "Role"
    assert label(NONE) == "none" and label(ANY) == "Any"
    facts = expected_families(T.union(T.list_of(T.union(INT, FLOAT)), NONE))
    assert facts.type_classes == ("list", "none")
    assert facts.none and facts.numeric_elements and facts.float_elements and facts.ordered
    pairs = expected_families(T.list_of(T.tuple_of([INT, INT])))
    assert pairs.numeric_pair and pairs.comparable_elements and not pairs.numeric_elements
    literal = expected_families(T.union(T.literal("a", "b"), NONE))
    assert literal.type_classes == ("none",)
    assert [a.classes for a in literal.arm_classes] == [("'a'", "'b'")]
    enum = expected_families(T.enum("Role", ("ADMIN", "MEMBER")))
    assert [(a.family, a.name, a.classes) for a in enum.arm_classes] == [
        ("enum", "Role", ("Role.ADMIN", "Role.MEMBER"))
    ]
    assert expected_families(None).arms == ()
