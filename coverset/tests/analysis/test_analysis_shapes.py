"""The input-class catalogue on hand-made observations, including the five README cases."""

from __future__ import annotations

import textwrap

import pytest
from analysis_helpers import obs

from coverset.analysis.functions import index_functions_detailed
from coverset.analysis.shapes import (
    Catalogue,
    ParamInfo,
    ShapeKeyError,
    annotation_index,
    function_input_cov,
    parse_shape_key,
    render_markdown,
    suggest_annotation,
)
from coverset.lang.base import FuncSampling, InputCov

DEMO = textwrap.dedent(
    """\
    def validate_email(email: str) -> str:
        return email


    def parse_duration(text: str) -> int:
        return 0


    def allocate_cents(total_cents: int, weights: list[float]) -> list[int]:
        return []


    def merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
        return intervals


    def untyped(value, *args, **kwargs):
        return value
    """
)
FUNCS = {d.region.qualname: d for d in index_functions_detailed("src/ledger/demo.py", DEMO)}


def _cov(name: str, observations, sampling=None):
    entry = FUNCS[name]
    annotations = dict(zip(entry.region.params, entry.annotations, strict=True))
    return function_input_cov(entry.region, observations, sampling, annotations=annotations)


def _missing(cov) -> set[str]:
    return {c.name for c in cov.missing}


def test_parse_shape_key() -> None:
    shape = parse_shape_key("str|ascii|kind:email/multi-label-domain|len:11-100")
    assert shape.type == "str" and shape.flags == {"ascii"}
    assert shape.has("kind", "email/multi-label-domain") and shape.has("len", "11-100")
    money = parse_shape_key("obj:ledger.util.money.Money|dataclass")
    assert (money.group, money.type_name, money.flags) == ("object", "Money", {"dataclass"})
    assert parse_shape_key("NoneType").type_name == "none"
    with pytest.raises(ShapeKeyError):
        parse_shape_key("")
    with pytest.raises(ShapeKeyError):
        parse_shape_key("int||pos")


def test_validate_email_never_sees_a_multi_label_domain() -> None:
    cov = _cov(
        "validate_email",
        [
            obs("email", "str|ascii|kind:email/plain|len:11-100", example="'a***@acme.com'"),
            obs("email", "str|ascii|len:2-10", example="'not-email'"),
            obs("email", "str|empty|len:0", example="''"),
        ],
    )
    assert "email:multi-label-domain" in _missing(cov)
    assert {"email:plain", "email:ascii", "email:empty"} <= cov.observed
    assert cov.pct == round(100 * 3 / len(cov.expected), 1)
    families = {c.name: c.family for c in cov.expected}
    assert families["email:multi-label-domain"] == "email"


def test_parse_duration_never_sees_a_multi_letter_unit() -> None:
    cov = _cov(
        "parse_duration",
        [
            obs("text", "str|ascii|digits|kind:quantity/single-unit|len:2-10"),
            obs("text", "str|ascii|digits|kind:quantity/multi-unit|len:2-10"),
            obs("text", "str|ascii|len:2-10"),
        ],
    )
    missing = _missing(cov)
    assert "text:multi-letter-unit" in missing
    assert {"text:decimal", "text:signed", "text:trailing-garbage"} <= missing
    assert "text:single-unit" not in missing


def test_allocate_cents_negative_total_and_zero_weight() -> None:
    cov = _cov(
        "allocate_cents",
        [
            obs("total_cents", "int|pos", example="100"),
            obs("total_cents", "int|zero", example="0"),
            obs("weights", "list|elem:int|len:2-10|order:sorted|dups", example="[1, 1, 1]"),
            obs("weights", "list|elem:float|len:2-10|order:unsorted", example="[0.5, 0.2, 0.3]"),
        ],
    )
    missing = _missing(cov)
    assert {
        "total_cents:negative",
        "weights:zero-element",
        "weights:all-zero",
        "weights:nan-element",
    } <= missing
    assert {
        "total_cents:zero",
        "total_cents:positive",
        "weights:unsorted",
        "weights:duplicates",
    } <= cov.observed
    hint = next(c.hypothesis_hint for c in cov.expected if c.name == "total_cents:negative")
    assert hint == "st.integers(max_value=-1)"


def test_merge_intervals_trusts_sorted_input() -> None:
    cov = _cov(
        "merge_intervals",
        [obs("intervals", "list|elem:tuple|len:2-10|order:sorted|pair:lt", example="[(1, 3), (2, 4)]")],
    )
    missing = _missing(cov)
    assert {"intervals:unsorted", "intervals:inverted-pair", "intervals:empty"} <= missing
    assert "intervals:multiple" in cov.observed


def test_mixed_pairs_do_not_count_as_inverted() -> None:
    cov = _cov("merge_intervals", [obs("intervals", "list|elem:tuple|len:2-10|order:sorted|pair:mixed")])
    assert "intervals:inverted-pair" in _missing(cov)
    cov = _cov("merge_intervals", [obs("intervals", "list|elem:tuple|len:1|order:n/a|pair:gt")])
    assert "intervals:inverted-pair" in cov.observed


def test_classes_only_join_when_observed_kind_present() -> None:
    catalogue = Catalogue()
    expected = catalogue.expected_classes(ParamInfo("n", "int"), [obs("n", "int|pos")])
    assert [c.name for c in expected] == ["n:negative", "n:zero", "n:positive"]
    assert catalogue.expected_classes(ParamInfo("n", "int"), []) == []


def test_annotation_arms_bool_literal_optional_enum() -> None:
    catalogue = Catalogue(enum_members=lambda name: ("RED", "GREEN") if name == "Color" else None)
    names = lambda p, ann, o: [c.name for c in catalogue.expected_classes(ParamInfo(p, ann), o)]  # noqa: E731
    assert names("flag", "bool", [obs("flag", "bool|true")]) == ["flag:true", "flag:false"]
    assert names("flag", "bool", []) == ["flag:true", "flag:false"]
    assert names("x", "int | str", [obs("x", "int|pos")]) == [
        "x:int",
        "x:str",
        "x:negative",
        "x:zero",
        "x:positive",
    ]
    assert names("name", "Optional[str]", []) == ["name:str", "name:none"]
    assert names("name", "str | None", [obs("name", "NoneType")]) == ["name:str", "name:none"]
    assert names("mode", "Literal['fast', 'slow']", []) == ["mode:'fast'", "mode:'slow'"]
    assert names("c", "Color", []) == ["c:Color.RED", "c:Color.GREEN"]
    observed = catalogue.function_input_cov(
        FUNCS["validate_email"].region,
        [obs("email", "str|ascii|len:2-10", example="'fast'")],
        None,
        annotations={"email": "Literal['fast', 'slow']"},
    )
    assert "email:'fast'" in observed.observed and "email:'slow'" not in observed.observed


def test_collisions_are_prefixed_by_kind() -> None:
    expected = Catalogue().expected_classes(
        ParamInfo("data", None), [obs("data", "str|empty|len:0"), obs("data", "list|len:0")]
    )
    names = {c.name for c in expected}
    assert {"data:str-empty", "data:container-empty"} <= names
    assert "data:empty" not in names
    observed = Catalogue().observed_classes([obs("data", "str|empty|len:0"), obs("data", "list|len:0")])
    assert {"data:str-empty", "data:container-empty"} <= observed


def test_unannotated_params_are_never_100_percent() -> None:
    cov = _cov("untyped", [obs("value", "bool|true"), obs("value", "bool|false")])
    assert cov.missing == []
    assert cov.pct is None
    assert "value unannotated: observed {bool}" in cov.note


def test_star_params_match_tracer_names() -> None:
    cov = _cov("untyped", [obs("*args", "tuple|len:0"), obs("**kwargs", "dict|len:1|key:str")])
    assert {"args:empty", "kwargs:single"} <= cov.observed


def test_never_exercised_and_sample_cap_notes() -> None:
    assert _cov("validate_email", []).note == "never exercised"
    assert _cov("validate_email", []).pct is None
    sampling = FuncSampling(FUNCS["parse_duration"].region.triple, 2048, ("t1", "t2"), True)
    cov = _cov(
        "parse_duration", [obs("text", "str|ascii|len:2-10", truncated=True, tests=("t1", "t2"))], sampling
    )
    assert cov.tests_truncated == 2
    assert "not observed in 2048 sampled calls (2 tests hit the sample cap)" in cov.note


def test_render_markdown_table() -> None:
    cov = _cov(
        "validate_email",
        [obs("email", "str|ascii|kind:email/plain|len:11-100", example="'a***@acme.com'")],
    )
    text = render_markdown(cov)
    lines = text.splitlines()
    assert lines[0] == "| Input class | Observed | Example or suggested Hypothesis strategy |"
    assert "| `email:plain` | yes | `'a***@acme.com'` |" in lines
    assert (
        '| `email:multi-label-domain` | **no** | `st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"])` |'
        in lines
    )
    assert lines[-1].startswith(f"2 of {len(cov.expected)} input classes observed")  # ascii, plain


def test_render_markdown_escapes_pipes_and_backticks() -> None:
    cov = _cov("validate_email", [obs("email", "str|ascii|len:2-10", example="'a|`b`'")])
    line = next(line for line in render_markdown(cov).splitlines() if line.startswith("| `email:ascii`"))
    assert line == "| `email:ascii` | yes | ``'a\\|`b`'`` |"


def test_suggest_annotation() -> None:
    assert suggest_annotation([obs("x", "int|pos"), obs("x", "float|nan")]) == "float"
    assert suggest_annotation([obs("x", "str|ascii|len:1"), obs("x", "NoneType")]) == "str | None"
    weights = [
        obs("w", "list|elem:int|len:2-10"),
        obs("w", "list|elem:float|len:2-10"),
        obs("w", "list|len:0"),
    ]
    assert suggest_annotation(weights) == "list[float]"
    assert suggest_annotation([obs("d", "dict|len:1|key:str|elem:int")]) == "dict[str, object]"
    assert suggest_annotation([obs("o", "obj:ledger.util.money.Money|dataclass")]) == "Money"
    assert suggest_annotation([]) == "object"


def test_param_input_cov_matches_lang_base_contract() -> None:
    result = Catalogue()([obs("n", "int|pos"), obs("return", "int|pos"), obs("<receiver>", "obj:a.B")])
    assert set(result) == {"n"}
    assert result["n"] == InputCov(("n:negative", "n:zero", "n:positive"), ("n:int", "n:positive"))
    assert result["n"].missing == ("n:negative", "n:zero")


def test_malformed_shape_keys_from_a_bundle_are_ignored() -> None:
    cov = _cov("parse_duration", [obs("text", "|bad"), obs("text", "str|ascii|len:2-10")])
    assert "text:ascii" in cov.observed
    assert suggest_annotation([obs("x", "int||pos"), obs("x", "int|pos")]) == "int"


MIXED_PATH = "src/pkg/mixed.py"
MIXED = textwrap.dedent(
    """\
    from typing import Literal


    def f(flag: bool, x: int | None, mode: Literal["a", "b"], extra, unseen: bool = False):
        return 1
    """
)


def test_ingest_view_agrees_with_function_input_cov() -> None:
    (region,) = [
        r for r in (d.region for d in index_functions_detailed(MIXED_PATH, MIXED)) if r.qualname == "f"
    ]
    func = region.triple
    observations = [
        obs("flag", "bool|true", func=func),
        obs("x", "int|pos", func=func),
        obs("mode", "str|ascii|len:1", example="'a'", func=func),
        obs("extra", "str|ascii|len:2-10", func=func),
    ]
    catalogue = Catalogue(annotations=annotation_index({MIXED_PATH: MIXED}))
    ingest = catalogue(observations)  # what facts.ingest.finalize_run stores
    ui = catalogue.function_input_cov(region, observations, None)  # annotations from the same table
    assert sum(len(c.expected) for c in ingest.values()) == len(ui.expected)
    assert sum(c.observed_expected for c in ingest.values()) == ui.observed_expected
    assert {m for c in ingest.values() for m in c.missing} == {c.name for c in ui.missing}
    assert {"x:none", "mode:'b'", "flag:false", "unseen:true", "unseen:false"} <= _missing(ui)
    assert catalogue.unannotated(observations) == ["extra"]
    explicit = _cov_with(region, observations, MIXED_PATH, MIXED)
    assert {c.name for c in explicit.missing} == _missing(ui)
    assert "x:none" not in {n for c in Catalogue()(observations).values() for n in c.expected}


def _cov_with(region, observations, path: str, source: str):
    detail = next(d for d in index_functions_detailed(path, source) if d.region == region)
    annotations = dict(zip(detail.region.params, detail.annotations, strict=True))
    return function_input_cov(region, observations, None, annotations=annotations)


def test_missing_reason_says_never_exercised_only_without_a_sample_cap() -> None:
    complete = _cov("validate_email", [obs("email", "str|ascii|kind:email/plain|len:11-100")])
    assert complete.missing and complete.missing_reason == "never exercised"
    capped = _cov(
        "validate_email",
        [obs("email", "str|ascii|kind:email/plain|len:11-100", truncated=True, tests=("t::a", "t::b"))],
        FuncSampling(("src/ledger/demo.py", "validate_email", 1), 2048, ("t::a", "t::b"), True),
    )
    assert capped.missing_reason == "not observed in 2048 sampled calls (2 tests hit the sample cap)"
