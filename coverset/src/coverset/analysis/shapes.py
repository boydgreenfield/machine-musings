"""The input-class catalogue: which kinds of argument a function's tests passed in, and which not.

A *shape key* (Addendum B grammar, ``int|pos``, ``str|ascii|kind:email/plain|len:11-100``) records
the features of one argument value; an *input class* is a named block of a parameter's input space
(``email:multi-label-domain``). Input-class coverage of a function is observed ÷ expected classes
over its parameters (each-choice coverage). Rules:

* Expectations come from data: a family's classes join a parameter's expected set only when at
  least one observed value has that kind (an ``int`` was seen, so negative, zero and positive are
  expected; an email-shaped string was seen, so the email sub-classes are expected).
* Element-summary classes are expected for sequences of numbers (zero, negative and NaN element,
  all zero, elements summing to zero), of comparable values (unsorted, duplicates) and of numeric
  pairs (inverted pair), judged from the observed element type or, when parseable, the
  annotation's element type.
* The annotation adds each ``Union`` arm, each ``Literal`` value, each ``Enum`` member (when the
  catalogue can resolve the enum and some observed example of the parameter names a member, so
  members are distinguishable), both ``bool`` values and the ``None`` of an ``Optional``.
* Unannotated parameters are never reported at 100%: when everything expected was observed, the
  percentage is None and the note says "unannotated: observed {...}".

The same derivation serves both consumers: ``facts.ingest.finalize_run`` calls the catalogue once
per function (:meth:`Catalogue.__call__`), and the UI and CLI call
:meth:`Catalogue.function_input_cov`. Annotations reach the ingest path through the catalogue's
``annotations`` table keyed by function triple (build it with :func:`annotation_index` over the
head commit's sources), so both report the same expected and observed classes. ``InputCov`` has no
room for the unannotated rule; :meth:`Catalogue.unannotated` names the parameters it applies to.

Class names are ``<param>:<class>``; when two kinds of one parameter would produce the same class
name (an empty ``str`` and an empty ``list``) both are prefixed with their kind (``str-empty``).

The example shown for an observed class is chosen by a stable rule (:func:`example_sort_key`): the
shortest repr, then the lexicographically smallest, then the smallest test nodeid, so tables
rendered from the same facts are byte-identical whatever order the observations arrive in.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

from coverset.analysis.functions import FunctionRegion, index_functions_detailed
from coverset.lang.base import FuncSampling, FuncTriple, InputCov, ShapeObs

__all__ = [
    "Catalogue",
    "FunctionInputCov",
    "InputClass",
    "ParamInfo",
    "ParsedShape",
    "ShapeKeyError",
    "annotation_index",
    "enum_index",
    "example_sort_key",
    "function_input_cov",
    "parse_shape_key",
    "render_markdown",
    "suggest_annotation",
]

NON_INPUT_PARAMS: Final = frozenset({"return", "<receiver>"})
NEVER_EXERCISED: Final = "never exercised"
"""What a missing class is called when no test of the function hit the sample cap."""
_NUMERIC: Final = frozenset({"int", "float"})
_COMPARABLE: Final = frozenset({"int", "float", "str", "bytes", "tuple"})
_CONTAINERS: Final = frozenset({"list", "tuple", "set", "frozenset", "dict"})
_ORDERED: Final = frozenset({"list", "tuple"})
_SEQUENCE_ANNOTATIONS: Final = frozenset(
    {"list", "List", "Sequence", "MutableSequence", "Iterable", "Collection", "tuple", "Tuple", "set", "Set",
     "frozenset", "FrozenSet", "AbstractSet", "MutableSet", "Iterator"}
)  # fmt: skip
_ORDERED_ANNOTATIONS: Final = frozenset({"list", "List", "Sequence", "MutableSequence", "tuple", "Tuple"})
_TYPE_ALIASES: Final = {
    "List": "list",
    "Tuple": "tuple",
    "Dict": "dict",
    "Set": "set",
    "FrozenSet": "frozenset",
}
_REPEATED_GROUPS: Final = ("str", "bytes", "container")
_ENUM_BASES: Final = frozenset({"Enum", "IntEnum", "StrEnum"})

type ParamAnnotations = Mapping[str, str | None]
"""Bare parameter name → annotation text (None when unannotated), in signature order."""


class ShapeKeyError(ValueError):
    """A shape key does not follow the Addendum B grammar."""


@dataclass(frozen=True, slots=True)
class ParsedShape:
    """A shape key split into its type token, plain flags and ``prefix:value`` attributes."""

    type: str
    flags: frozenset[str]
    attrs: Mapping[str, frozenset[str]]

    def has(self, prefix: str, value: str) -> bool:
        """Whether the key carries the attribute token ``prefix:value``."""
        return value in self.attrs.get(prefix, ())

    @property
    def group(self) -> str:
        """The value-kind group: ``container`` for the five container types, else the type token."""
        if self.type in _CONTAINERS:
            return "container"
        return "object" if self.type.startswith("obj:") else self.type

    @property
    def type_name(self) -> str:
        """Short type name: ``none`` for NoneType, the class name for ``obj:`` types."""
        if self.type == "NoneType":
            return "none"
        return self.type.rsplit(".", 1)[-1] if self.type.startswith("obj:") else self.type


@dataclass(frozen=True, slots=True)
class ParamInfo:
    """A parameter name and its annotation text (``ast.unparse`` form) or None."""

    name: str
    annotation: str | None


@dataclass(frozen=True, slots=True)
class InputClass:
    """One named block of a parameter's input space."""

    name: str
    family: str
    description: str
    hypothesis_hint: str

    @property
    def param(self) -> str:
        """The parameter part of ``name``."""
        return self.name.split(":", 1)[0]


@dataclass(frozen=True, slots=True)
class FunctionInputCov:
    """Input-class coverage of one function over all its parameters."""

    expected: list[InputClass]
    observed: set[str]
    missing: list[InputClass]
    pct: float | None
    tests_truncated: int
    note: str
    examples: dict[str, str] = field(default_factory=dict)
    """Redacted example value per observed class."""
    tests: dict[str, tuple[str, ...]] = field(default_factory=dict)
    """Tests that passed in a value of each observed class."""
    missing_reason: str = NEVER_EXERCISED
    """How to label a missing class: "never exercised" only when ``tests_truncated == 0``, else
    "not observed in N sampled calls (K tests hit the sample cap)"."""

    @property
    def observed_expected(self) -> int:
        """Number of expected classes that were observed."""
        return sum(1 for c in self.expected if c.name in self.observed)


def parse_shape_key(key: str) -> ParsedShape:
    """Parse ``<type>|<feature>|...``; ``prefix:value`` features go to ``attrs``."""
    tokens = key.split("|")
    if not tokens[0]:
        raise ShapeKeyError(f"shape key has no type token: {key!r}")
    attrs: dict[str, set[str]] = defaultdict(set)
    flags: set[str] = set()
    for token in tokens[1:]:
        if not token:
            raise ShapeKeyError(f"empty feature token in shape key: {key!r}")
        prefix, sep, value = token.partition(":")
        if sep:
            attrs[prefix].add(value)
        else:
            flags.add(token)
    return ParsedShape(tokens[0], frozenset(flags), {k: frozenset(v) for k, v in attrs.items()})


# --- class definitions -------------------------------------------------------------------------

_INT_HINTS: Final = {
    "negative": "st.integers(max_value=-1)",
    "zero": "st.just(0)",
    "positive": "st.integers(min_value=1)",
}
_FLOAT_HINTS: Final = {
    "negative": "st.floats(max_value=0, exclude_max=True)",
    "zero": "st.sampled_from([0.0, -0.0])",
    "positive": "st.floats(min_value=0, exclude_min=True)",
}
_CLASS_TEXT: Final[Mapping[str, tuple[str, str]]] = {
    "none": ("None", "st.none()"),
    "true": ("True", "st.just(True)"),
    "false": ("False", "st.just(False)"),
    "negative": ("a negative number", ""),
    "zero": ("zero", ""),
    "positive": ("a positive number", ""),
    "nan": ("NaN", 'st.just(float("nan"))'),
    "inf": ("positive or negative infinity", 'st.sampled_from([float("inf"), float("-inf")])'),
    "str-empty": ('the empty string ""', 'st.just("")'),
    "whitespace": ('a whitespace-only string, e.g. " \\t"', 'st.sampled_from([" ", "\\t", "\\n "])'),
    "ascii": ("a non-empty ASCII string", "st.text(st.characters(max_codepoint=127), min_size=1)"),
    "unicode": (
        'a string with non-ASCII characters, e.g. "café"',
        "st.text(st.characters(min_codepoint=128), min_size=1)",
    ),
    "ws-edge": ('leading or trailing whitespace, e.g. " x "', 'st.text(min_size=1).map(lambda s: f" {s} ")'),
    "bytes-empty": ('empty bytes b""', 'st.just(b"")'),
    "non-empty": ("non-empty bytes", "st.binary(min_size=1)"),
    "container-empty": ("an empty container", "st.just([])"),
    "single": ("exactly one element", "st.lists({elem}, min_size=1, max_size=1)"),
    "multiple": ("two or more elements", "st.lists({elem}, min_size=2)"),
    "plain": ("a plain address, e.g. bob@acme.com", "st.emails()"),
    "multi-label-domain": (
        "a domain with three or more labels, e.g. bob@eng.acme.com",
        'st.sampled_from(["bob@eng.acme.com", "ops@mail.eu.example.org"])',
    ),
    "plus-tag": ("a plus-tagged local part, e.g. bob+news@acme.com", 'st.just("bob+news@acme.com")'),
    "uppercase": ("upper-case letters, e.g. Bob@Acme.COM", 'st.just("Bob@Acme.COM")'),
    "idn": ("an internationalised domain, e.g. bob@bücher.de", 'st.just("bob@bücher.de")'),
    "single-unit": ('one amount and unit, e.g. "90s"', 'st.sampled_from(["90s", "15m"])'),
    "multi-unit": ('several amount-unit pairs, e.g. "1h30m"', 'st.just("1h30m")'),
    "multi-letter-unit": (
        'a unit longer than one letter, e.g. "100ms"',
        'st.sampled_from(["100ms", "2min", "3hr"])',
    ),
    "decimal": ('a decimal amount, e.g. "1.5h"', 'st.just("1.5h")'),
    "signed": ('a signed amount, e.g. "-5m"', 'st.sampled_from(["-5m", "+5m"])'),
    "trailing-garbage": ('trailing text after the last unit, e.g. "5m!"', 'st.just("5m!")'),
    "numeric-string": ('digits in a string, e.g. "42"', "st.integers().map(str)"),
    "iso-date": ('an ISO date, e.g. "2026-10-11"', "st.dates().map(str)"),
    "url": ("a URL", 'st.just("https://example.com/path?q=1")'),
    "zero-element": ("a zero element, e.g. [3, 0]", "st.lists({elem}, min_size=1).map(lambda xs: xs + [0])"),
    "negative-element": (
        "a negative element, e.g. [3, -1]",
        "st.lists({elem}, min_size=1).map(lambda xs: xs + [-1])",
    ),
    "nan-element": ("a NaN element", 'st.lists({elem}, min_size=1).map(lambda xs: xs + [float("nan")])'),
    "all-zero": ("only zero elements, e.g. [0, 0]", "st.lists(st.just(0), min_size=1)"),
    "sum-zero": (
        "elements that sum to zero, e.g. [2, -2]",
        "st.lists({elem}, min_size=1, max_size=16).map(lambda xs: xs + [-x for x in xs])",
    ),
    "unsorted": (
        "elements out of order",
        "st.lists({elem}, min_size=2, unique=True).map(lambda xs: sorted(xs)[::-1])",
    ),
    "duplicates": ("a repeated element", "st.lists({elem}, min_size=1).map(lambda xs: xs + xs[:1])"),
    "inverted-pair": (
        "a pair whose first item exceeds its second, e.g. [(5, 2)]",
        "st.lists(st.tuples(st.integers(), st.integers()).filter(lambda p: p[0] > p[1]), min_size=1)",
    ),
}
_EMAIL: Final = ("plain", "multi-label-domain", "plus-tag", "uppercase", "idn")
_QUANTITY: Final = ("single-unit", "multi-unit", "multi-letter-unit", "decimal", "signed", "trailing-garbage")
_STR_KINDS: Final = {"numeric": "numeric-string", "iso-date": "iso-date", "url": "url"}
_TYPE_HINTS: Final = {
    "none": "st.none()",
    "bool": "st.booleans()",
    "int": "st.integers()",
    "float": "st.floats()",
    "str": "st.text()",
    "bytes": "st.binary()",
    "list": "st.lists(st.integers())",
    "tuple": "st.tuples(st.integers())",
    "dict": "st.dictionaries(st.text(), st.integers())",
    "set": "st.sets(st.integers())",
    "frozenset": "st.frozensets(st.integers())",
}


@dataclass(frozen=True, slots=True)
class _Arm:
    """One parsed annotation arm: base name, generic arguments and literal values."""

    name: str
    args: tuple[_Arm, ...] = ()
    literals: tuple[object, ...] = ()


@dataclass(slots=True)
class _ClassRow:
    group: str
    cls: str
    family: str


@dataclass(slots=True)
class _ParamState:
    """Everything the catalogue derives for one parameter."""

    param: str
    shapes: list[tuple[ParsedShape, ShapeObs]]
    arms: list[_Arm] | None
    expected: list[InputClass] = field(default_factory=list)
    observed: dict[str, set[str]] = field(default_factory=dict)
    examples: dict[str, str] = field(default_factory=dict)
    example_keys: dict[str, tuple[int, str, str]] = field(default_factory=dict)


def example_sort_key(example: str, test: str | None) -> tuple[int, str, str]:
    """The stable order examples are chosen by: shortest repr, smallest repr, smallest test nodeid.

    The first example under this key is the one shown for a class (in :func:`render_markdown`, in
    :class:`FunctionInputCov` and in ``facts.queries.shapes_for_function`` rows), so the README
    tables do not depend on test order, shard layout or which run recorded a value first.
    """
    return (len(example), example, test or "\U0010ffff")


class Catalogue:
    """Derives expected and observed input classes from shape observations and annotations.

    ``annotations`` maps a function triple ``(path, qualname, firstlineno)`` to its parameters'
    annotations (see :func:`annotation_index`); it lets the per-function call used at ingest apply
    the same annotation rules as :meth:`function_input_cov`. ``enum_members`` optionally resolves
    an annotation name (``"Color"``) to its member names so ``Enum`` annotations contribute one
    class per member; members are observed through example reprs such as ``<Color.RED: 1>``.
    """

    def __init__(
        self,
        enum_members: Callable[[str], Sequence[str] | None] | None = None,
        *,
        annotations: Mapping[FuncTriple, ParamAnnotations] | None = None,
    ) -> None:
        self._enum_members = enum_members
        self._annotations = annotations or {}

    def __call__(self, observations: Sequence[ShapeObs]) -> dict[str, InputCov]:
        """Per-parameter :class:`~coverset.lang.base.InputCov` for one function's observations.

        This is the ``catalogue`` callable of ``facts.ingest.finalize_run``; the function's
        annotations come from the ``annotations`` table, looked up by the observations' triple.
        """
        return self.param_input_cov(observations, self._annotations_for(observations))

    def annotations_of(self, func: FuncTriple) -> ParamAnnotations | None:
        """The parameter annotations recorded for one function triple (None when unknown)."""
        return self._annotations.get(func)

    def expected_classes(self, param: ParamInfo, observed: Sequence[ShapeObs]) -> list[InputClass]:
        """Expected input classes of ``param`` given its observations, in catalogue order."""
        return self._state(param, observed).expected

    def observed_classes(self, observed: Iterable[ShapeObs]) -> set[str]:
        """Every class name (``<param>:<class>``) some observation fell into."""
        names: set[str] = set()
        for name, obs_list in _group_by_param(observed).items():
            names |= set(self._state(ParamInfo(name, None), obs_list).observed)
        return names

    def param_input_cov(
        self, observations: Sequence[ShapeObs], annotations: ParamAnnotations | None = None
    ) -> dict[str, InputCov]:
        """Per-parameter expected and observed class names for one function's observations.

        Parameters named in ``annotations`` are included even when never observed (their
        annotation alone can make classes expected), exactly as in :meth:`function_input_cov`.
        """
        notes = {_bare(k): v for k, v in (annotations or {}).items()}
        result: dict[str, InputCov] = {}
        for state in self._states(observations, list(notes), notes):
            observed = tuple(sorted(state.observed))
            result[state.param] = InputCov(tuple(c.name for c in state.expected), observed)
        return result

    def unannotated(self, observations: Sequence[ShapeObs]) -> list[str]:
        """Observed parameters of one function that have no annotation (never shown as 100%)."""
        notes = {_bare(k): v for k, v in (self._annotations_for(observations) or {}).items()}
        return sorted(name for name in _group_by_param(observations) if notes.get(name) is None)

    def function_input_cov(
        self,
        region: FunctionRegion,
        observations: Sequence[ShapeObs],
        sampling: FuncSampling | None,
        *,
        annotations: ParamAnnotations | None = None,
    ) -> FunctionInputCov:
        """Input-class coverage of ``region`` from the observations of that function.

        ``annotations`` maps parameter names to annotation text (see
        :func:`coverset.analysis.functions.index_functions_detailed`); when omitted, the
        catalogue's ``annotations`` table is consulted for ``region.triple``.
        """
        if annotations is None:
            annotations = self._annotations.get(region.triple)
        notes = {_bare(k): v for k, v in (annotations or {}).items()}
        expected: list[InputClass] = []
        observed: dict[str, set[str]] = {}
        examples: dict[str, str] = {}
        unannotated: list[str] = []
        for state in self._states(observations, [_bare(p) for p in region.params], notes):
            expected += state.expected
            observed.update(state.observed)
            examples.update(state.examples)
            if notes.get(state.param) is None and state.shapes:
                kinds = sorted({shape.type_name for shape, _ in state.shapes})
                unannotated.append(f"{state.param} unannotated: observed {{{', '.join(kinds)}}}")
        missing = [c for c in expected if c.name not in observed]
        truncated = _tests_truncated(observations, sampling)
        pct = round(100.0 * (len(expected) - len(missing)) / len(expected), 1) if expected else None
        if pct == 100.0 and unannotated:
            pct = None
        sampled = _sampling_note(sampling, truncated)
        note_parts = list(unannotated)
        if not observations:
            note_parts.append(sampled or NEVER_EXERCISED)
        elif missing and truncated:
            note_parts.append(sampled)
        return FunctionInputCov(
            expected=expected,
            observed=set(observed),
            missing=missing,
            pct=pct,
            tests_truncated=truncated,
            note="; ".join(note_parts),
            examples=examples,
            tests={name: tuple(sorted(tests)) for name, tests in observed.items()},
            missing_reason=sampled or NEVER_EXERCISED,
        )

    def _annotations_for(self, observations: Sequence[ShapeObs]) -> ParamAnnotations | None:
        return self._annotations.get(observations[0].func) if observations else None

    def _states(
        self, observations: Sequence[ShapeObs], params: Sequence[str], notes: ParamAnnotations
    ) -> list[_ParamState]:
        """One state per parameter: ``params`` in order, then other observed ones sorted."""
        by_param = _group_by_param(observations)
        order = [*params, *sorted(set(by_param) - set(params))]
        return [
            self._state(ParamInfo(name, notes.get(name)), by_param.get(name, []))
            for name in dict.fromkeys(order)
        ]

    # --- derivation ----------------------------------------------------------------------------

    def _state(self, param: ParamInfo, observed: Sequence[ShapeObs]) -> _ParamState:
        name = _bare(param.name)
        shapes = [(shape, o) for o in observed if (shape := _parse_or_none(o.shape_key)) is not None]
        arms = _parse_annotation(param.annotation) if param.annotation else None
        state = _ParamState(name, shapes, arms)
        rows = self._expected_rows(state)
        renames = _collision_renames(rows)
        for row in rows:
            cls = renames.get((row.group, row.cls), row.cls)
            state.expected.append(self._input_class(name, cls, row.family, state))
        for shape, obs in shapes:
            for group, cls in _shape_classes(shape) + self._annotation_hits(state, obs):
                full = f"{name}:{renames.get((group, cls), cls)}"
                state.observed.setdefault(full, set()).update(obs.tests)
                if obs.example is not None:
                    key = example_sort_key(obs.example, obs.example_test)
                    if full not in state.example_keys or key < state.example_keys[full]:
                        state.example_keys[full] = key
                        state.examples[full] = obs.example
        return state

    def _expected_rows(self, state: _ParamState) -> list[_ClassRow]:
        shapes = [s for s, _ in state.shapes]
        groups = {s.group for s in shapes}
        types = {s.type for s in shapes}
        arms = state.arms or []
        arm_names = {_TYPE_ALIASES.get(a.name, a.name) for a in arms}
        elem_arms = [e for a in arms if a.name in _SEQUENCE_ANNOTATIONS for e in _element_arms(a)]
        rows: list[_ClassRow] = []

        def add(group: str, family: str, *classes: str) -> None:
            rows.extend(_ClassRow(group, c, family) for c in classes)

        if len(arms) > 1:
            for arm in arms:
                if not arm.literals:
                    add("type", "type", _arm_class(arm))
        if ("NoneType" in types or "None" in arm_names) and not any(r.cls == "none" for r in rows):
            add("none", "none", "none")
        if "bool" in types or "bool" in arm_names:
            add("bool", "bool", "true", "false")
        if groups & _NUMERIC:
            add("number", "number", "negative", "zero", "positive")
            if "float" in types or "float" in arm_names:
                add("number", "number", "nan", "inf")
        if "str" in groups:
            add("str", "str", "empty", "whitespace", "ascii", "unicode", "ws-edge")
            kinds = {k.split("/", 1)[0] for s in shapes for k in s.attrs.get("kind", ())}
            if "email" in kinds:
                add("email", "email", *_EMAIL)
            if "quantity" in kinds:
                add("quantity", "quantity", *_QUANTITY)
            for kind, cls in _STR_KINDS.items():
                if kind in kinds:
                    add(cls, cls, cls)
        if "bytes" in groups:
            add("bytes", "bytes", "empty", "non-empty")
        if "container" in groups:
            add("container", "container", "empty", "single", "multiple")
            elems = {e for s in shapes if s.group == "container" for e in s.attrs.get("elem", ())}
            elem_names = {_TYPE_ALIASES.get(a.name, a.name) for a in elem_arms}
            if elems & _NUMERIC or (elem_names and elem_names <= _NUMERIC):
                add("elements", "elements", "zero-element", "negative-element", "all-zero", "sum-zero")
                if "float" in elems or "float" in elem_names:
                    add("elements", "elements", "nan-element")
            ordered = any(s.type in _ORDERED for s in shapes) or any(
                a.name in _ORDERED_ANNOTATIONS for a in arms
            )
            if ordered and (elems & _COMPARABLE or (elem_names and elem_names <= _COMPARABLE)):
                add("order", "order", "unsorted", "duplicates")
            if any("pair" in s.attrs for s in shapes) or any(_is_numeric_pair(e) for e in elem_arms):
                add("pairs", "pairs", "inverted-pair")
        for arm in arms:
            for value in arm.literals:
                add("literal", "literal", repr(value))
            if self._enum_members and not arm.literals and arm.name not in _TYPE_HINTS:
                members = self._enum_members(arm.name) or ()
                # Members are expected only when the data can tell them apart: some example of the
                # parameter names a member (``<Color.RED: 1>``). The shipped tracer records reprs of
                # exact builtins only, so without such an example every member would read as never
                # exercised even when the tests pass it.
                if members and any(f"{arm.name}." in (obs.example or "") for _, obs in state.shapes):
                    for member in members:
                        add("enum", "enum", f"{arm.name}.{member}")
        return _dedupe(rows)

    def _annotation_hits(self, state: _ParamState, obs: ShapeObs) -> list[tuple[str, str]]:
        """Literal and enum classes an observation's example value shows it fell into."""
        if not state.arms or obs.example is None:
            return []
        hits: list[tuple[str, str]] = []
        for arm in state.arms:
            hits += [("literal", repr(v)) for v in arm.literals if repr(v) == obs.example]
            if self._enum_members and not arm.literals:
                for member in self._enum_members(arm.name) or ():
                    if f"{arm.name}.{member}" in obs.example:
                        hits.append(("enum", f"{arm.name}.{member}"))
        return hits

    def _input_class(self, param: str, cls: str, family: str, state: _ParamState) -> InputClass:
        description, hint = _class_text(cls, family, state)
        return InputClass(f"{param}:{cls}", family, description, hint)


def function_input_cov(
    region: FunctionRegion,
    observations: Sequence[ShapeObs],
    sampling: FuncSampling | None,
    *,
    annotations: Mapping[str, str | None] | None = None,
    catalogue: Catalogue | None = None,
) -> FunctionInputCov:
    """Input-class coverage of ``region`` with the default (or given) catalogue."""
    return (catalogue or Catalogue()).function_input_cov(
        region, observations, sampling, annotations=annotations
    )


def annotation_index(sources: Mapping[str, str]) -> dict[FuncTriple, dict[str, str | None]]:
    """Parameter annotations of every function in ``sources`` (path → text), by function triple.

    Feed it the head commit's measured source files and pass the result to :class:`Catalogue` so
    ingest applies the annotation rules. Keys of the inner mapping are bare parameter names.
    """
    index: dict[FuncTriple, dict[str, str | None]] = {}
    for path, source in sources.items():
        for item in index_functions_detailed(path, source):
            region = item.region
            if region.kind in ("function", "method"):
                index[region.triple] = {
                    _bare(p): a for p, a in zip(region.params, item.annotations, strict=True)
                }
    return index


def enum_index(sources: Mapping[str, str]) -> dict[str, tuple[str, ...]]:
    """Member names of every ``Enum``/``IntEnum``/``StrEnum`` class defined in ``sources``, by class name.

    Found with the AST only (nothing is imported): a class whose base is one of those names, or a
    class in ``sources`` already found to be an enum, contributes the names assigned in its body
    (``RED = 1``, ``RED: int = 1``), private names and ``_ignore_`` excluded. A class name defined
    twice with different members is left out (the annotation alone cannot say which one is meant).
    Pass the result's ``get`` as :class:`Catalogue`'s ``enum_members``.
    """
    classes: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = []
    for path in sorted(sources):
        try:
            tree = ast.parse(sources[path])
        except (SyntaxError, ValueError, RecursionError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = tuple(_name(b) for b in node.bases)
                classes.append((node.name, bases, _enum_member_names(node)))
    enums: set[str] = set()
    changed = True
    while changed:  # a subclass of an enum found in the sources is an enum too
        changed = False
        for name, bases, _ in classes:
            if name not in enums and any(b in _ENUM_BASES or b in enums for b in bases):
                enums.add(name)
                changed = True
    found: dict[str, tuple[str, ...]] = {}
    ambiguous: set[str] = set()
    for name, _, members in classes:
        if name not in enums or not members:
            continue
        if name in found and found[name] != members:
            ambiguous.add(name)
        found.setdefault(name, members)
    return {name: members for name, members in found.items() if name not in ambiguous}


def _enum_member_names(node: ast.ClassDef) -> tuple[str, ...]:
    names: list[str] = []
    for item in node.body:
        targets: list[ast.expr] = []
        if isinstance(item, ast.Assign):
            targets = list(item.targets)
        elif isinstance(item, ast.AnnAssign) and item.value is not None:
            targets = [item.target]
        for target in targets:
            if isinstance(target, ast.Name) and not target.id.startswith("_") and target.id not in names:
                names.append(target.id)
    return tuple(names)


def render_markdown(cov: FunctionInputCov) -> str:
    """A GitHub-flavoured markdown table: class | observed? | example or suggested strategy."""
    lines = [
        "| Input class | Observed | Example or suggested Hypothesis strategy |",
        "|---|---|---|",
    ]
    for cls in cov.expected:
        if cls.name in cov.observed:
            example = cov.examples.get(cls.name)
            detail = _code(example) if example is not None else "(no example recorded)"
            lines.append(f"| {_code(cls.name)} | yes | {detail} |")
        else:
            lines.append(f"| {_code(cls.name)} | **no** | {_code(cls.hypothesis_hint)} |")
    summary = f"{cov.observed_expected} of {len(cov.expected)} input classes observed"
    if cov.pct is not None:
        summary += f" ({cov.pct:g}%)"
    lines += ["", summary + (f"; {_escape(cov.note)}" if cov.note else "") + "."]
    return "\n".join(lines) + "\n"


def suggest_annotation(observations: Iterable[ShapeObs]) -> str:
    """The narrowest annotation consistent with one parameter's observed shapes (``object`` if none).

    ``int`` folds into ``float`` and ``bool`` into either, following the numeric tower; element
    types of containers are merged across observations; ``None`` comes last.
    """
    scalars: set[str] = set()
    elems: dict[str, set[str]] = {}
    keys: set[str] = set()
    for obs in observations:
        shape = _parse_or_none(obs.shape_key)
        if shape is None:
            continue
        if shape.type in _CONTAINERS:
            elems.setdefault(shape.type, set()).update(_elem_name(e) for e in shape.attrs.get("elem", ()))
            keys.update(_elem_name(k) for k in shape.attrs.get("key", ()))
        else:
            scalars.add("None" if shape.type == "NoneType" else shape.type_name)
    arms = [_container_annotation(kind, elems[kind], keys) for kind in sorted(elems)]
    arms += sorted(_narrow(scalars - {"None"}))
    if "None" in scalars:
        arms.append("None")
    return " | ".join(arms) or "object"


# --- helpers -----------------------------------------------------------------------------------


def _bare(param: str) -> str:
    return param.lstrip("*")


def _parse_or_none(key: str) -> ParsedShape | None:
    """Parse a shape key from an untrusted bundle; malformed keys are ignored."""
    try:
        return parse_shape_key(key)
    except ShapeKeyError:
        return None


def _group_by_param(observations: Iterable[ShapeObs]) -> dict[str, list[ShapeObs]]:
    by_param: dict[str, list[ShapeObs]] = defaultdict(list)
    for obs in observations:
        if obs.param not in NON_INPUT_PARAMS:
            by_param[_bare(obs.param)].append(obs)
    return by_param


def _shape_classes(shape: ParsedShape) -> list[tuple[str, str]]:
    """``(group, class)`` pairs one observed value falls into."""
    found: list[tuple[str, str]] = [("type", shape.type_name)]
    f, kind = shape.flags, shape.type
    if kind == "NoneType":
        found.append(("none", "none"))
    elif kind == "bool":
        found += [("bool", c) for c in ("true", "false") if c in f]
    elif kind in _NUMERIC:
        names = {"neg": "negative", "zero": "zero", "pos": "positive", "nan": "nan", "inf": "inf"}
        found += [("number", cls) for token, cls in names.items() if token in f]
    elif kind == "str":
        found += [("str", c) for c in ("empty", "whitespace", "ascii", "unicode") if c in f]
        if "ws-edge" in f:
            found.append(("str", "ws-edge"))
        for value in shape.attrs.get("kind", ()):
            family, _, sub = value.partition("/")
            if family == "email":
                found.append(("email", sub or "plain"))
            elif family == "quantity" and sub:
                found.append(("quantity", sub))
            elif family in _STR_KINDS:
                found.append((_STR_KINDS[family], _STR_KINDS[family]))
    elif kind == "bytes":
        found.append(("bytes", "empty" if shape.has("len", "0") else "non-empty"))
    elif kind in _CONTAINERS:
        found.append(("container", _size_class(shape)))
        numeric = shape.attrs.get("elem", frozenset()) & _NUMERIC
        tokens = {"any_zero": "zero-element", "any_neg": "negative-element", "any_nan": "nan-element"}
        if numeric:
            found += [("elements", cls) for token, cls in tokens.items() if token in f]
            if "all_zero" in f:
                found.append(("elements", "all-zero"))
            if "sum_zero" in f:
                found.append(("elements", "sum-zero"))
        if kind in _ORDERED and (shape.has("order", "unsorted") or shape.has("order", "reverse")):
            found.append(("order", "unsorted"))
        if "dups" in f:
            found.append(("order", "duplicates"))
        if shape.has("pair", "gt"):  # "mixed" may be lt+eq only, so it proves no inversion
            found.append(("pairs", "inverted-pair"))
    return found


def _size_class(shape: ParsedShape) -> str:
    if shape.has("len", "0"):
        return "empty"
    return "single" if shape.has("len", "1") else "multiple"


def _collision_renames(rows: Sequence[_ClassRow]) -> dict[tuple[str, str], str]:
    """Prefix class names that two value-kind groups of one parameter would both produce."""
    owners: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        owners[row.cls].add(row.group)
    return {(g, cls): f"{g}-{cls}" for cls, groups in owners.items() if len(groups) > 1 for g in groups}


def _dedupe(rows: list[_ClassRow]) -> list[_ClassRow]:
    seen: set[tuple[str, str]] = set()
    result: list[_ClassRow] = []
    for row in rows:
        if (row.group, row.cls) not in seen:
            seen.add((row.group, row.cls))
            result.append(row)
    return result


def _class_text(cls: str, family: str, state: _ParamState) -> tuple[str, str]:
    """Description and Hypothesis hint for one class of ``state``'s parameter."""
    prefix, sep, rest = cls.partition("-")
    base = rest if sep and prefix in _REPEATED_GROUPS else cls
    if family == "type":
        return f"a value of type {cls}", _TYPE_HINTS.get(cls, f"st.from_type({cls})")
    if family == "literal":
        return f"the literal {cls}", f"st.just({cls})"
    if family == "enum":
        return f"the enum member {cls}", f"st.just({cls})"
    if family == "number":
        is_float = any(s.type == "float" for s, _ in state.shapes) and not any(
            s.type == "int" for s, _ in state.shapes
        )
        hints = _FLOAT_HINTS if is_float else _INT_HINTS
        description, hint = _CLASS_TEXT[base]
        return description, hints.get(base, hint)
    key = f"{family}-{base}" if base == "empty" else base
    description, hint = _CLASS_TEXT[key]
    return description, hint.replace("{elem}", _element_strategy(state))


def _element_strategy(state: _ParamState) -> str:
    elems = {e for s, _ in state.shapes for e in s.attrs.get("elem", ())}
    if "float" in elems:
        return "st.floats(0, 100)"
    if "str" in elems:
        return "st.text()"
    if "tuple" in elems:
        return "st.tuples(st.integers(), st.integers())"
    return "st.integers(0, 100)"


def _tests_truncated(observations: Sequence[ShapeObs], sampling: FuncSampling | None) -> int:
    """Distinct tests in truncated observation rows (all sampled tests if only the function says so)."""
    tests = {t for o in observations if o.truncated for t in o.tests}
    if not tests and sampling is not None and sampling.truncated:
        tests = set(sampling.tests)
    return len(tests)


def _sampling_note(sampling: FuncSampling | None, truncated: int) -> str:
    if not truncated:
        return ""
    calls = sampling.calls if sampling is not None else 0
    return f"not observed in {calls} sampled calls ({truncated} tests hit the sample cap)"


def _elem_name(token: str) -> str:
    return {"NoneType": "None", "mixed": "object"}.get(token, token)


def _narrow(types: set[str]) -> set[str]:
    """Apply the numeric tower: ``int`` folds into ``float``, ``bool`` into ``int``/``float``."""
    narrowed = set(types)
    if "float" in narrowed:
        narrowed -= {"int", "bool"}
    if "int" in narrowed:
        narrowed.discard("bool")
    return narrowed


def _union_text(types: set[str]) -> str:
    narrowed = _narrow(types)
    if "object" in narrowed:
        return "object"
    return " | ".join(sorted(narrowed - {"None"}) + (["None"] if "None" in narrowed else []))


def _container_annotation(kind: str, elems: set[str], keys: set[str]) -> str:
    inner = _union_text(elems)
    if kind == "dict":
        return f"dict[{_union_text(keys) or 'object'}, object]"
    if kind == "tuple":
        return f"tuple[{inner}, ...]" if inner else "tuple"
    return f"{kind}[{inner}]" if inner else kind


def _parse_annotation(text: str) -> list[_Arm] | None:
    """Top-level union arms of an annotation, or None when it does not parse."""
    try:
        node = ast.parse(text, mode="eval").body
    except (SyntaxError, ValueError, RecursionError):
        return None
    return _union_arms(node)


def _union_arms(node: ast.expr) -> list[_Arm]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union_arms(node.left) + _union_arms(node.right)
    if isinstance(node, ast.Subscript):
        base = _name(node.value)
        items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if base == "Union":
            return [arm for item in items for arm in _union_arms(item)]
        if base == "Optional":
            return [arm for item in items for arm in _union_arms(item)] + [_Arm("None")]
        if base == "Annotated" and items:
            return _union_arms(items[0])
        if base == "Literal":
            values = tuple(i.value for i in items if isinstance(i, ast.Constant))
            return [_Arm("Literal", literals=values)]
        return [_Arm(base, tuple(_single_arm(i) for i in items))]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        parsed = _parse_annotation(node.value)
        return parsed if parsed is not None else [_Arm(node.value)]
    return [_Arm(_name(node))]


def _single_arm(node: ast.expr) -> _Arm:
    arms = _union_arms(node)
    if len(arms) == 1:
        return arms[0]
    return _Arm("Union", tuple(arms))


def _name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Constant) and node.value is None:
        return "None"
    if isinstance(node, ast.Constant) and node.value is Ellipsis:
        return "..."
    return ast.unparse(node)


def _element_arms(arm: _Arm) -> list[_Arm]:
    """Element type arms of a sequence annotation (``tuple[X, ...]`` counts; fixed tuples do not)."""
    if not arm.args:
        return []
    if arm.name in ("tuple", "Tuple"):
        if len(arm.args) == 2 and arm.args[1].name == "...":
            return _flatten(arm.args[0])
        return []
    return _flatten(arm.args[0])


def _flatten(arm: _Arm) -> list[_Arm]:
    return list(arm.args) if arm.name == "Union" else [arm]


def _is_numeric_pair(arm: _Arm) -> bool:
    if arm.name not in ("tuple", "Tuple") or len(arm.args) != 2:
        return False
    return all(_TYPE_ALIASES.get(a.name, a.name) in _NUMERIC for a in arm.args)


def _arm_class(arm: _Arm) -> str:
    if arm.name == "None":
        return "none"
    return _TYPE_ALIASES.get(arm.name, arm.name)


def _escape(text: str) -> str:
    """Escape markdown-significant characters in table text."""
    out = text.replace("\\", "\\\\")
    for char in "|*_`[]<>":
        out = out.replace(char, "\\" + char)
    return out.replace("\n", " ")


def _code(text: str) -> str:
    """A code span with a fence longer than any backtick run inside; pipes escaped for tables."""
    body = text.replace("\n", " ").replace("|", "\\|")
    longest = run = 0
    for char in body:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if body.startswith("`") or body.endswith("`") else ""
    return f"{fence}{pad}{body}{pad}{fence}"
