"""The catalogue on descriptors: per-function coverage, notes, the ingest view, display text."""

from __future__ import annotations

from model_helpers import ADD, T1, T2, small_bundle

from coverset_model.catalogue import (
    NEVER_EXERCISED,
    Catalogue,
    describe,
    function_catalogue_from_types,
    function_input_cov,
    render_markdown,
)
from coverset_model.neutral import FuncSampling, FunctionRegion, ShapeObs, func_key
from coverset_model.types import TypeDescriptor as T

INT, FLOAT, STR, NONE = T.scalar("int"), T.scalar("float"), T.scalar("str"), T.scalar("none")
REGION = FunctionRegion(
    func_key("src/rates.ts", "rateFor", 0), "src/rates.ts", "rateFor", 0, 5, 6, 9, "b", "s",
    ("zone", "weights", "retries"), "function",
)  # fmt: skip
TYPES = {
    "zone": T.literal("domestic", "eu"),
    "weights": T.list_of(INT),
    "retries": T.union(INT, NONE),
    "return": INT,
}


def _obs(param: str, key: str, example: str | None = None, *, truncated: bool = False) -> ShapeObs:
    return ShapeObs(REGION.triple, param, key, "t::a", 1, ("t::a",), truncated, example, "t::a")


def test_function_input_cov_from_descriptors() -> None:
    observations = [
        _obs("zone", "str|ascii|len:2-10", "'eu'"),
        _obs("weights", "list|elem:int|len:2-10|order:sorted"),
        _obs("retries", "int|pos", "3"),
    ]
    cov = function_input_cov(REGION, observations, None, annotations=TYPES)
    names = [c.name for c in cov.expected]
    assert names[:2] == ["zone:empty", "zone:whitespace"] and "zone:'domestic'" in names
    assert {"zone:'eu'", "weights:multiple", "retries:positive", "retries:int"} <= cov.observed
    assert {"zone:'domestic'", "weights:zero-element", "weights:unsorted", "retries:none"} <= {
        c.name for c in cov.missing
    }
    assert cov.missing_reason == NEVER_EXERCISED and cov.tests_truncated == 0
    assert "return" not in {c.param for c in cov.expected}


def test_ingest_view_matches_and_ignores_return() -> None:
    observations = [_obs("retries", "int|pos"), _obs("return", "int|pos")]
    catalogue = Catalogue(annotations={REGION.triple: TYPES})
    per_param = catalogue(observations)
    assert set(per_param) == {"zone", "weights", "retries"}
    assert per_param["retries"].expected == (
        "retries:int",
        "retries:none",
        "retries:negative",
        "retries:zero",
        "retries:positive",
    )
    assert catalogue.unannotated(observations) == []
    cov = catalogue.function_input_cov(REGION, observations, None)
    assert sum(len(c.expected) for c in per_param.values()) == len(cov.expected)


def test_capped_sampling_labels_missing_classes() -> None:
    observations = [_obs("retries", "int|pos", truncated=True)]
    sampling = FuncSampling(REGION.triple, 2048, ("t::a",), True, "sample")
    cov = function_input_cov(REGION, observations, sampling, annotations=TYPES, tests_called=("t::a", "t::b"))
    assert cov.tests_truncated == 1
    assert cov.missing_reason == "not observed in 2048 sampled calls (1 test hit the sample cap)"


def test_unannotated_parameters_are_never_100_percent() -> None:
    cov = function_input_cov(REGION, [_obs("retries", "bool|true"), _obs("retries", "bool|false")], None)
    assert cov.pct is None and "retries unannotated: observed {bool}" in cov.note


def test_function_catalogue_from_bundle_types() -> None:
    bundle = small_bundle()
    found = function_catalogue_from_types(ADD, bundle.types[ADD.triple])
    assert found.annotations == {"a": "int", "b": "int | None"} and found.returns is None
    cov = found.input_cov(
        ADD, [o for o in bundle.shapes if o.func == ADD.triple], bundle.sampling[0], tests_called=(T1, T2)
    )
    assert "b:none" in {c.name for c in cov.missing}
    assert render_markdown(cov).splitlines()[0].startswith("| Input class |")
    empty = function_catalogue_from_types(ADD, None)
    assert empty.annotations == {"a": None, "b": None}


def test_describe() -> None:
    assert describe(None) is None
    assert describe(T.union(T.list_of(FLOAT), NONE)) == "list[float] | None"
    assert describe(T.tuple_of(elem=INT)) == "tuple[int, ...]"
    assert describe(T.tuple_of([INT, STR])) == "tuple[int, str]"
    assert describe(T.dict_of(STR)) == "dict[str, Any]"
    assert describe(T.literal("a", 1)) == "Literal['a', 1]"
    assert describe(T.enum("pkg.Role", ("A",))) == "pkg.Role"


def test_literal_values_match_the_python_repr_and_the_json_form() -> None:
    for example in ("'eu'", '"eu"'):
        cov = function_input_cov(
            REGION, [_obs("zone", "str|ascii|len:2-10", example)], None, annotations=TYPES
        )
        assert "zone:'eu'" in cov.observed, example
        assert "zone:'domestic'" in {c.name for c in cov.missing}
    flags = {"flag": T.literal(True, None), "zone": T.literal("domestic", "eu")}
    region = FunctionRegion(
        func_key("src/f.ts", "f", 0), "src/f.ts", "f", 0, 1, 2, 3, "b", "s", ("flag",), "function"
    )
    observations = [
        ShapeObs(region.triple, "flag", "bool|true", "t::a", 1, ("t::a",), False, "true", "t::a"),
        ShapeObs(region.triple, "flag", "NoneType", "t::a", 1, ("t::a",), False, "null", "t::a"),
    ]
    cov = function_input_cov(region, observations, None, annotations=flags)
    assert {"flag:True", "flag:None"} <= set(cov.observed)


def test_enum_members_match_whole_tokens_only() -> None:
    types = {"role": T.enum("Role", ("admin", "admin_ro"))}
    region = FunctionRegion(
        func_key("src/r.py", "g", 0), "src/r.py", "g", 0, 1, 2, 3, "b", "s", ("role",), "function"
    )

    def obs(example: str) -> ShapeObs:
        return ShapeObs(region.triple, "role", "object", "t::a", 1, ("t::a",), False, example, "t::a")

    cov = function_input_cov(region, [obs("<Role.admin_ro: 2>")], None, annotations=types)
    assert "role:Role.admin_ro" in cov.observed
    assert "role:Role.admin" in {c.name for c in cov.missing}
    cov = function_input_cov(region, [obs("<pkg.Role.admin: 1>")], None, annotations=types)
    assert "role:Role.admin" in cov.observed and "role:Role.admin_ro" not in cov.observed
    cov = function_input_cov(region, [obs("<XRole.admin: 1>")], None, annotations=types)
    assert not {c.name for c in cov.expected} & {"role:Role.admin", "role:Role.admin_ro"}
