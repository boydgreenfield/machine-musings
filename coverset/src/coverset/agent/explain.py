"""Words for the machine vocabulary: shape keys, and fast-check arbitraries for input classes.

:func:`explain_shape_key` reads a key with the neutral model's grammar
(:mod:`coverset_model.shape_keys`) and says what each token means, so an agent never has to guess
what ``list|dups|elem:int|len:2-10|order:sorted`` describes. :func:`fast_check_hint` gives the
TypeScript equivalent of a missing class's Hypothesis strategy for the classes the catalogue
defines; for anything else it returns None and the tools say to translate the Hypothesis strategy.
"""

from __future__ import annotations

import re
from typing import Any, Final

from coverset_model.shape_keys import ShapeKeyError, parse, validate

__all__ = ["explain_shape_key", "fast_check_hint", "grammar_text"]

_TYPES: Final = {
    "NoneType": "None (null)",
    "bool": "a boolean",
    "int": "an integer",
    "float": "a floating-point number",
    "str": "a string",
    "bytes": "a byte string",
    "list": "a list",
    "tuple": "a tuple",
    "dict": "a dict (mapping)",
    "set": "a set",
    "frozenset": "a frozenset",
}
_FLAGS: Final = {
    "true": "the value True",
    "false": "the value False",
    "neg": "negative",
    "zero": "zero",
    "pos": "positive",
    "big": "wider than 64 bits",
    "nan": "NaN",
    "inf": "positive infinity",
    "inf-neg": "negative infinity",
    "integral": "a whole number stored as a float (e.g. 2.0)",
    "empty": "empty",
    "whitespace": "only whitespace",
    "ascii": "ASCII characters only",
    "unicode": "contains non-ASCII characters",
    "ws-edge": "leading or trailing whitespace",
    "digits": "contains digits",
    "any_zero": "at least one element is zero",
    "any_neg": "at least one element is negative",
    "any_nan": "at least one element is NaN",
    "any_inf": "at least one element is infinite",
    "all_zero": "every element is zero",
    "sum_zero": "the elements sum to zero",
    "dups": "has a repeated element",
    "dataclass": "a dataclass instance",
    "pydantic": "a pydantic model instance",
}
_ATTRS: Final = {
    "len": "length {v}",
    "elem": "elements of type {v}",
    "key": "keys of type {v}",
    "order": "element order: {v}",
    "pair": "pairs whose first item is {v} the second (lt/eq/gt/mixed)",
    "kind": "recognised as {v}",
}
_KINDS: Final = {
    "email/plain": "a plain email address",
    "email/multi-label-domain": "an email address whose domain has three or more labels",
    "email/plus-tag": "an email address with a +tag in the local part",
    "email/uppercase": "an email address with upper-case letters",
    "email/idn": "an email address with an internationalised domain",
    "quantity/single-unit": "a duration-like quantity with one amount and unit (90s)",
    "quantity/multi-unit": "a quantity with several amount-unit pairs (1h30m)",
    "quantity/multi-letter-unit": "a quantity whose unit is longer than one letter (100ms)",
    "quantity/decimal": "a quantity with a decimal amount (1.5h)",
    "quantity/signed": "a quantity with a sign (-5m)",
    "quantity/trailing-garbage": "a quantity followed by stray text (5m!)",
    "numeric": "digits only (a number in a string)",
    "iso-date": "an ISO date",
    "url": "a URL",
}


def grammar_text() -> str:
    """The grammar in a paragraph (the ``explain_shape_key`` result's ``grammar``)."""
    return (
        "A shape key is `<type>` followed by `|`-separated feature tokens in sorted order. The type is "
        "one of NoneType, bool, int, float, str, bytes, list, tuple, dict, set, frozenset or "
        "`obj:<module>.<Class>`. A plain token is a flag (`neg`, `empty`, `dups`); a `prefix:value` "
        "token is an attribute (`len:2-10`, `elem:int`, `kind:email/plain`, `order:unsorted`). Lengths "
        "are bucketed (0, 1, 2-10, 11-100, 101-1000, >1000). Keys describe values without storing them; "
        "the catalogue maps keys to input classes such as `email:multi-label-domain`."
    )


def explain_shape_key(key: str) -> dict[str, Any]:
    """``key`` in words: its type, each feature, and its problems under the version-1 grammar."""
    problems = validate(key)
    try:
        shape = parse(key)
    except ShapeKeyError as exc:
        return {"key": key, "valid": False, "problems": [str(exc)], "grammar": grammar_text()}
    if shape.type.startswith("obj:"):
        type_words = f"an instance of {shape.type[4:]}"
    else:
        type_words = _TYPES.get(shape.type, f"an unknown type token {shape.type!r}")
    features: list[dict[str, str]] = []
    for flag in sorted(shape.flags):
        features.append(
            {"token": flag, "meaning": _FLAGS.get(flag, "a feature this grammar does not define")}
        )
    for prefix in sorted(shape.attrs):
        for value in sorted(shape.attrs[prefix]):
            if prefix == "kind":
                meaning = _KINDS.get(value, f"recognised as {value}")
            elif prefix in ("elem", "key"):
                meaning = _ATTRS[prefix].format(v=_TYPES.get(value, value))
            else:
                meaning = _ATTRS.get(prefix, prefix + ": {v}").format(v=value)
            features.append({"token": f"{prefix}:{value}", "meaning": meaning})
    words = type_words
    if features:
        words += ": " + "; ".join(f["meaning"] for f in features)
    return {
        "key": key,
        "valid": not problems,
        "problems": problems,
        "type": shape.type,
        "in_words": words,
        "features": features,
        "grammar": grammar_text(),
    }


_FAST_CHECK: Final = {
    "negative": "fc.integer({ max: -1 })",
    "zero": "fc.constant(0)",
    "positive": "fc.integer({ min: 1 })",
    "nan": "fc.constant(Number.NaN)",
    "inf": "fc.constantFrom(Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY)",
    "none": "fc.constant(null)",
    "true": "fc.constant(true)",
    "false": "fc.constant(false)",
    "str-empty": 'fc.constant("")',
    "empty": 'fc.constant("")',
    "whitespace": 'fc.constantFrom(" ", "\\t", "\\n ")',
    "ascii": "fc.string({ minLength: 1 })",
    "unicode": "fc.fullUnicodeString({ minLength: 1 }).filter((s) => /[^\\x00-\\x7f]/.test(s))",
    "ws-edge": "fc.string({ minLength: 1 }).map((s) => ` ${s} `)",
    "container-empty": "fc.constant([])",
    "single": "fc.array(fc.integer(), { minLength: 1, maxLength: 1 })",
    "multiple": "fc.array(fc.integer(), { minLength: 2 })",
    "plain": "fc.emailAddress()",
    "multi-label-domain": 'fc.constantFrom("bob@eng.acme.com", "ops@mail.eu.example.org")',
    "plus-tag": 'fc.constant("bob+news@acme.com")',
    "uppercase": 'fc.constant("Bob@Acme.COM")',
    "idn": 'fc.constant("bob@bücher.de")',
    "single-unit": 'fc.constantFrom("90s", "15m")',
    "multi-unit": 'fc.constant("1h30m")',
    "multi-letter-unit": 'fc.constantFrom("100ms", "2min", "3hr")',
    "decimal": 'fc.constant("1.5h")',
    "signed": 'fc.constantFrom("-5m", "+5m")',
    "trailing-garbage": 'fc.constant("5m!")',
    "numeric-string": "fc.integer().map(String)",
    "iso-date": "fc.date().map((d) => d.toISOString().slice(0, 10))",
    "url": "fc.webUrl()",
    "zero-element": "fc.array(fc.integer(), { minLength: 1 }).map((xs) => [...xs, 0])",
    "negative-element": "fc.array(fc.integer(), { minLength: 1 }).map((xs) => [...xs, -1])",
    "nan-element": "fc.array(fc.double(), { minLength: 1 }).map((xs) => [...xs, Number.NaN])",
    "all-zero": "fc.array(fc.constant(0), { minLength: 1 })",
    "sum-zero": "fc.array(fc.integer(), { minLength: 1, maxLength: 16 }).map((xs) => [...xs, ...xs.map((x) => -x)])",
    "unsorted": "fc.uniqueArray(fc.integer(), { minLength: 2 }).map((xs) => [...xs].sort((a, b) => b - a))",
    "duplicates": "fc.array(fc.integer(), { minLength: 1 }).map((xs) => [...xs, xs[0]])",
}


_JUST = re.compile(r"^st\.just\((.*)\)$", re.DOTALL)
_SAMPLED = re.compile(r"^st\.sampled_from\(\[(.*)\]\)$", re.DOTALL)
_INT_BOUND = re.compile(r"^st\.integers\((min|max)_value=(-?\d+)\)$")
_SIMPLE = re.compile(r"""^(?:"[^"\\]*"|'[^'\\]*'|-?\d+(?:\.\d+)?|\[\]|True|False|None)$""")
_JS_WORDS: Final = {"True": "true", "False": "false", "None": "null"}


def _js_literal(text: str) -> str | None:
    text = text.strip()
    if not _SIMPLE.match(text):
        return None
    return _JS_WORDS.get(text, text)


def fast_check_hint(class_name: str, hypothesis: str | None = None) -> str | None:
    """A fast-check arbitrary for a missing input class, or None when there is no direct
    translation (the tools then say to translate the Hypothesis strategy).

    The class's Hypothesis strategy is translated first when it is a plain constant, a choice of
    constants or a one-sided integer range (``st.just("")`` → ``fc.constant("")``,
    ``st.integers(max_value=-1)`` → ``fc.integer({ max: -1 })``); otherwise the catalogue class's
    short name is looked up (``unsorted``, ``sum-zero``, ``multi-label-domain``, ...).
    """
    if hypothesis:
        text = hypothesis.strip()
        if match := _JUST.match(text):
            literal = _js_literal(match.group(1))
            if literal is not None:
                return f"fc.constant({literal})"
        if match := _SAMPLED.match(text):
            parts = [_js_literal(p) for p in match.group(1).split(",")]
            if parts and all(parts):
                return f"fc.constantFrom({', '.join(p for p in parts if p)})"
        if match := _INT_BOUND.match(text):
            return f"fc.integer({{ {match.group(1)}: {match.group(2)} }})"
    short = class_name.split(":", 1)[-1]
    if short == "empty":  # a string's and a container's "empty" share the name: only the strategy tells
        return None
    return _FAST_CHECK.get(short)
