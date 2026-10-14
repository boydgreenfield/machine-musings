"""Rust: a light source indexer and the :class:`RustAdapter`.

coverset does not run a Rust suite inside its own workers (there is no Rust environment builder),
so the adapter serves the two paths that do work: a local run (``coverset rust run``, which drives
``cargo nextest`` and LLVM source-based coverage through :mod:`coverset.lang.rust_llvm`) and the
upload of the neutral bundle that run writes. What the adapter adds over the generic
:class:`~coverset.lang.neutral.NeutralAdapter` is that it *reads Rust*: function regions, their
hashes and declared types come from the sources, so a diff between two commits can be classified
per function (``changed_functions``) without a bundle for either side.

The indexer is deliberately not a Rust parser. It tokenizes (comments, strings, raw strings, char
literals and lifetimes are told apart, so braces inside them never count) and then walks items by
brace matching: ``mod``, ``impl``, ``trait``, ``fn``, ``enum`` are recognised at item position,
everything else is skipped as one group. That is enough for what coverage needs:

* every ``fn`` with a body, named by its in-file path (``Ledger::post``,
  ``<LedgerError as fmt::Display>::fmt``, ``tests::splits``; nested functions as ``outer::inner``),
  with ``def_line`` on its first attribute, ``body_start`` on the first token inside the braces and
  ``end_line`` on the closing brace;
* which items are test-only (``#[test]``-like attributes, ``#[cfg(test)]`` and everything inside
  such an item): their lines become ``excluded`` and their functions are not regions;
* ``mod name;`` declarations, so a test named ``money::tests::splits`` in the library's test binary
  resolves to ``src/money.rs`` (the 2018 module layout; ``#[path]`` is not followed);
* parameter names and type text, turned into neutral type descriptors (``i64`` is ``int``,
  ``&str`` is ``str``, ``Option<T>`` a union with ``none``, ``Vec<T>``/``&[T]`` a list, an enum
  declared in the crate an ``enum`` with its variants).

Hashes are taken over the token texts, not the raw slice, so reformatting (``rustfmt``) or editing a
comment does not change a function's ``body_hash``; an attribute or signature change moves only
``sig_hash``. Macro-generated functions (``macro_rules!`` bodies, ``proptest!``) are invisible to
the indexer; their lines still count, attributed to whatever region surrounds them.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Final

from coverset.facts.untrusted import BundleError, BundleLimits
from coverset.lang.base import EnvSpec, FunctionChange, FunctionRegion, NeutralBundle
from coverset.lang.neutral import NeutralAdapter, NotRunnableError
from coverset_model.neutral import func_key
from coverset_model.types import ANY, TypeDescriptor

__all__ = [
    "CrateIndex",
    "FileIndex",
    "RustAdapter",
    "RustFn",
    "Token",
    "index_file",
    "index_source",
    "tokenize",
    "type_descriptor",
]

_PUNCT3: Final = ("..=", "...", "<<=", ">>=")
_PUNCT2: Final = ("::", "->", "=>", "..", "==", "!=", "&&", "||", "+=", "-=", "*=", "/=", "%=", "^=", "|=")
_OPEN: Final = {"(": ")", "[": "]", "{": "}"}
_MODIFIERS: Final = frozenset({"pub", "async", "unsafe", "extern", "default", "safe", "const"})
_TEST_ATTRS: Final = frozenset({"test", "bench", "rstest", "test_case", "quickcheck", "wasm_bindgen_test"})
_INTS: Final = frozenset(
    {"i8", "i16", "i32", "i64", "i128", "isize", "u8", "u16", "u32", "u64", "u128", "usize"}
)
_NONZERO: Final = frozenset({f"NonZero{t.upper()}" for t in _INTS} | {"NonZero"})
_STRS: Final = frozenset({"str", "String", "char", "OsStr", "OsString", "CStr", "CString"})
_LISTS: Final = frozenset({"Vec", "VecDeque", "LinkedList", "BinaryHeap", "SmallVec", "ArrayVec"})
_SETS: Final = frozenset({"HashSet", "BTreeSet", "IndexSet"})
_MAPS: Final = frozenset({"HashMap", "BTreeMap", "IndexMap"})
_WRAPPERS: Final = frozenset({"Box", "Rc", "Arc", "Cow", "RefCell", "Cell", "Mutex", "RwLock", "Pin"})
MAX_SOURCE_BYTES: Final = 4 * 1024 * 1024
"""Files larger than this are not indexed (generated code; the regions would be noise)."""


# -- tokens -----------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Token:
    """One token: ``kind`` is ``ident``, ``punct``, ``lit`` (string, char or number) or ``life``."""

    kind: str
    text: str
    line: int


def _is_ident_start(c: str) -> bool:
    return c == "_" or c.isalpha()


def _is_ident_char(c: str) -> bool:
    return c == "_" or c.isalnum()


def tokenize(source: str) -> list[Token]:
    """Split Rust ``source`` into tokens, dropping whitespace and comments.

    String-like literals (``"..."``, ``r#"..."#``, ``b"..."``, ``c"..."``, ``'x'``, ``b'x'``) become
    one ``lit`` token with the text ``"<str>"``/``"<char>"`` (their content never matters to the
    indexer, and keeping it out keeps hashes of long literals cheap); a lifetime or label is a
    ``life`` token. An unterminated literal or comment runs to the end of the file, as rustc would
    report, rather than raising: the indexer must survive half-edited sources.
    """
    out: list[Token] = []
    i, n, line = 0, len(source), 1
    while i < n:
        c = source[i]
        if c == "\n":
            line += 1
            i += 1
        elif c.isspace():
            i += 1
        elif source.startswith("//", i):
            j = source.find("\n", i)
            i = n if j < 0 else j
        elif source.startswith("/*", i):
            depth, j = 1, i + 2
            while j < n and depth:
                if source.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif source.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                else:
                    j += 1
            line += source.count("\n", i, j)
            i = j
        elif (raw := _raw_string_end(source, i)) is not None:
            out.append(Token("lit", "<str>", line))
            line += source.count("\n", i, raw)
            i = raw
        elif c == '"' or (c in "bc" and source.startswith('"', i + 1)):
            j = i + (1 if c == '"' else 2)
            while j < n and source[j] != '"':
                j += 2 if source[j] == "\\" else 1
            out.append(Token("lit", "<str>", line))
            line += source.count("\n", i, j)
            i = j + 1
        elif c == "'" or (c == "b" and source.startswith("'", i + 1)):
            start = i + (1 if c == "'" else 2)
            if start < n and source[start] == "\\":
                j = start + 2
                while j < n and source[j] != "'" and source[j] != "\n":
                    j += 1
                out.append(Token("lit", "<char>", line))
                i = j + 1
            elif start + 1 < n and source[start + 1] == "'":
                out.append(Token("lit", "<char>", line))
                i = start + 2
            else:  # a lifetime or loop label: 'a, 'static, 'outer
                j = start
                while j < n and _is_ident_char(source[j]):
                    j += 1
                out.append(Token("life", source[i:j], line))
                i = max(j, i + 1)
        elif c.isdigit():
            j = i + 1
            while j < n and (_is_ident_char(source[j]) or (source[j] == "." and _decimal_point(source, j))):
                j += 1
            out.append(Token("lit", source[i:j], line))
            i = j
        elif _is_ident_start(c):
            j = i + 1
            if c == "r" and source.startswith("#", j) and j + 1 < n and _is_ident_start(source[j + 1]):
                j += 1  # a raw identifier, r#type
            while j < n and _is_ident_char(source[j]):
                j += 1
            out.append(Token("ident", source[i:j].removeprefix("r#"), line))
            i = j
        else:
            for width, table in ((3, _PUNCT3), (2, _PUNCT2)):
                if source[i : i + width] in table:
                    out.append(Token("punct", source[i : i + width], line))
                    i += width
                    break
            else:
                out.append(Token("punct", c, line))
                i += 1
    return out


def _decimal_point(source: str, j: int) -> bool:
    """A ``.`` inside a number literal (``1.5``), not a range (``1..2``) or a method call (``1.max``)."""
    return j + 1 < len(source) and source[j + 1].isdigit()


def _raw_string_end(source: str, i: int) -> int | None:
    """The index after a raw string literal starting at ``i`` (``r"..."``, ``br#"..."#``, ``cr"..."``)."""
    j = i
    if source[j] in "bc":
        j += 1
    if not source.startswith("r", j):
        return None
    j += 1
    hashes = 0
    while j < len(source) and source[j] == "#":
        hashes, j = hashes + 1, j + 1
    if not source.startswith('"', j):
        return None
    close = '"' + "#" * hashes
    end = source.find(close, j + 1)
    return len(source) if end < 0 else end + len(close)


# -- the index --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Param:
    """One parameter: its pattern (``amount``, ``(a, b)``) and its type as token texts."""

    name: str
    type_tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RustFn:
    """One ``fn`` with a body, as the indexer found it (see the module docstring for the lines)."""

    qualname: str
    def_line: int
    sig_line: int
    """The line of the first token after the attributes (``pub``, ``fn``): where LLVM's function
    record starts, used to match :mod:`coverset.lang.rust_llvm`'s records to regions."""
    body_start: int
    end_line: int
    kind: str
    params: tuple[Param, ...]
    returns: tuple[str, ...]
    generics: tuple[str, ...]
    is_test: bool
    self_type: str | None
    body_hash: str
    sig_hash: str


@dataclass(frozen=True, slots=True)
class ModDecl:
    """``mod name;`` (a module in another file), at in-file path ``scope``."""

    scope: tuple[str, ...]
    name: str
    is_test: bool


@dataclass(slots=True)
class FileIndex:
    """What the indexer found in one file."""

    fns: list[RustFn] = field(default_factory=list)
    mods: list[ModDecl] = field(default_factory=list)
    enums: dict[str, tuple[str, ...]] = field(default_factory=dict)
    test_spans: list[tuple[int, int]] = field(default_factory=list)
    n_lines: int = 0

    def excluded_lines(self) -> set[int]:
        """Every line inside a test-only item (attribute line to closing brace)."""
        return {line for start, end in self.test_spans for line in range(start, end + 1)}


@dataclass(frozen=True, slots=True)
class _Scope:
    path: tuple[str, ...] = ()
    kind: str = "mod"
    """``mod`` (file or inline module), ``impl``, ``trait`` or ``fn``."""
    is_test: bool = False
    self_type: str | None = None
    """The implementing type's written name inside an ``impl`` block (what ``Self`` means)."""


def _blake(text: str) -> str:
    return hashlib.blake2b(text.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()


def _match(tokens: Sequence[Token], i: int, end: int) -> int:
    """Index of the bracket closing the one at ``i`` (``end - 1`` when it never closes)."""
    opener = tokens[i].text
    closer = _OPEN[opener]
    depth = 0
    for j in range(i, end):
        text = tokens[j].text
        if tokens[j].kind != "punct":
            continue
        if text == opener:
            depth += 1
        elif text == closer:
            depth -= 1
            if depth == 0:
                return j
    return end - 1


def _skip_angle(tokens: Sequence[Token], i: int, end: int) -> int:
    """Index after the ``<...>`` group starting at ``i`` (``->`` and ``=>`` are single tokens)."""
    depth = 0
    j = i
    while j < end:
        text = tokens[j].text
        if text == "<":
            depth += 1
        elif text == ">":
            depth -= 1
            if depth == 0:
                return j + 1
        elif text in _OPEN:
            j = _match(tokens, j, end)
        j += 1
    return end


def _attr_is_test(texts: Sequence[str]) -> bool:
    """``#[test]``, ``#[tokio::test]``, ``#[rstest]``... or ``#[cfg(test)]``/``#[cfg(all(test, x))]``
    (never ``cfg(not(test))``)."""
    if not texts:
        return False
    if texts[0] == "cfg":
        return "test" in texts and "not" not in texts
    path = texts[: texts.index("(")] if "(" in texts else texts
    idents = [t for t in path if t != "::"]
    return bool(idents) and idents[-1] in _TEST_ATTRS


def _split_top(tokens: Sequence[Token], sep: str = ",") -> list[list[Token]]:
    """Split on ``sep`` outside every bracket and angle group."""
    parts: list[list[Token]] = [[]]
    depth = angle = 0
    for t in tokens:
        if t.kind == "punct":
            if t.text in _OPEN:
                depth += 1
            elif t.text in (")", "]", "}"):
                depth -= 1
            elif t.text == "<":
                angle += 1
            elif t.text == ">" and angle:
                angle -= 1
            elif t.text == sep and depth == 0 and angle == 0:
                parts.append([])
                continue
        parts[-1].append(t)
    return [p for p in parts if p]


def _params(tokens: Sequence[Token]) -> tuple[Param, ...]:
    found: list[Param] = []
    for part in _split_top(tokens):
        texts = [t.text for t in part]
        colon = next(
            (k for k, t in enumerate(part) if t.text == ":" and _depth_zero(part, k)),
            None,
        )
        pattern = texts if colon is None else texts[:colon]
        while pattern and pattern[0] == "#":  # an attribute on the parameter
            close = pattern.index("]") if "]" in pattern else len(pattern) - 1
            pattern = pattern[close + 1 :]
        if "self" in pattern and len([p for p in pattern if p not in ("&", "mut")]) <= 2:
            continue  # self, &self, &mut self, mut self, &'a self, self: Box<Self>
        name = "".join(p for p in pattern if p not in ("mut", "ref")) or "_"
        found.append(Param(name, tuple(texts[colon + 1 :]) if colon is not None else ()))
    return tuple(found)


def _depth_zero(part: Sequence[Token], k: int) -> bool:
    depth = 0
    for t in part[:k]:
        if t.text in _OPEN:
            depth += 1
        elif t.text in (")", "]", "}"):
            depth -= 1
    return depth == 0


def _type_name(tokens: Sequence[str]) -> str:
    """The written name of a type: path segments without generic arguments or references
    (``&'a mut crate::money::Ledger<T>`` is ``crate::money::Ledger``)."""
    out: list[str] = []
    depth = 0
    for text in tokens:
        if text == "<":
            depth += 1
        elif text == ">":
            depth = max(depth - 1, 0)
        elif depth == 0 and text not in ("&", "&&", "mut", "dyn", "const") and not text.startswith("'"):
            out.append(text)
    return "".join(out) or "?"


def _generic_names(tokens: Sequence[Token], i: int, end: int) -> tuple[tuple[str, ...], int]:
    """The type parameter names of the ``<...>`` group at ``i`` and the index after it."""
    close = _skip_angle(tokens, i, end)
    names: list[str] = []
    for part in _split_top(tokens[i + 1 : close - 1]):
        if part[0].kind == "ident" and part[0].text != "const":
            names.append(part[0].text)
        elif part[0].text == "const" and len(part) > 1:
            names.append(part[1].text)
    return tuple(names), close


class _Indexer:
    """Walks the tokens of one file and fills a :class:`FileIndex`."""

    def __init__(self, tokens: list[Token], n_lines: int) -> None:
        self.tokens = tokens
        self.index = FileIndex(n_lines=n_lines)
        self.seen: dict[str, int] = {}

    def run(self) -> FileIndex:
        self.items(0, len(self.tokens), _Scope())
        return self.index

    def items(self, start: int, end: int, scope: _Scope) -> None:
        tokens = self.tokens
        i = start
        attrs: list[tuple[int, list[str]]] = []
        while i < end:
            t = tokens[i]
            if t.text == "#" and i + 1 < end:
                j = i + 1
                inner = tokens[j].text == "!"
                if inner:
                    j += 1
                if j < end and tokens[j].text == "[":
                    close = _match(tokens, j, end)
                    if not inner:
                        attrs.append((t.line, [x.text for x in tokens[j + 1 : close]]))
                    elif _attr_is_test([x.text for x in tokens[j + 1 : close]]) and scope.path == ():
                        self.index.test_spans.append((1, self.index.n_lines))  # #![cfg(test)] file
                    i = close + 1
                    continue
            if t.text == ";":
                attrs = []
                i += 1
                continue
            first_line = attrs[0][0] if attrs else t.line
            own = any(_attr_is_test(texts) for _, texts in attrs)
            i = self.item(i, end, scope, first_line, scope.is_test or own)
            if own and not scope.is_test:  # the outermost test-only item: its lines are excluded
                self.index.test_spans.append((first_line, tokens[min(i, end) - 1].line))
            attrs = []

    def item(self, i: int, end: int, scope: _Scope, first_line: int, is_test: bool) -> int:
        """Handle the item starting at ``i``; returns the index after it."""
        tokens = self.tokens
        sig_line = tokens[i].line
        j = i
        while j < end and tokens[j].text in _MODIFIERS:
            if tokens[j].text == "pub" and j + 1 < end and tokens[j + 1].text == "(":
                j = _match(tokens, j + 1, end)
            elif tokens[j].text == "extern" and j + 1 < end and tokens[j + 1].kind == "lit":
                j += 1
            elif (
                tokens[j].text == "const"
                and j + 1 < end
                and tokens[j + 1].text
                not in (
                    "fn",
                    "unsafe",
                    "async",
                    "extern",
                )
            ):
                break  # a const item, not a const fn
            j += 1
        if j >= end:
            return end
        kw = tokens[j].text
        if kw == "fn" and j + 1 < end:
            return self.function(i, j, end, scope, first_line, sig_line, is_test)
        if kw == "mod" and j + 1 < end:
            return self.module(j, end, scope, first_line, is_test)
        if kw == "impl":
            return self.impl(j, end, scope, first_line, is_test)
        if kw == "trait" or (kw == "auto" and j + 1 < end and tokens[j + 1].text == "trait"):
            return self.trait(j + (kw == "auto"), end, scope, first_line, is_test)
        if kw == "enum" and j + 1 < end:
            return self.enum(j, end, first_line, is_test)
        if kw == "macro_rules":
            k = j + 1
            while k < end and tokens[k].text not in _OPEN:
                k += 1
            close = _match(tokens, k, end) if k < end else end - 1
            if close + 1 < end and tokens[close + 1].text == ";":
                close += 1
            return close + 1
        return self.skip(j, end, first_line, is_test)

    def skip(self, j: int, end: int, first_line: int, is_test: bool) -> int:
        """Skip one item or statement: to a ``;`` outside brackets, or through one ``{...}``."""
        tokens = self.tokens
        k = j
        while k < end:
            text = tokens[k].text
            if text in ("(", "["):
                k = _match(tokens, k, end) + 1
                continue
            if text == "{":
                close = _match(tokens, k, end)
                if close + 1 < end and tokens[close + 1].text == ";":
                    close += 1
                return close + 1
            if text == ";":
                return k + 1
            k += 1
        return end

    def function(
        self, i: int, j: int, end: int, scope: _Scope, first_line: int, sig_line: int, is_test: bool
    ) -> int:
        tokens = self.tokens
        name = tokens[j + 1].text
        k = j + 2
        generics: tuple[str, ...] = ()
        if k < end and tokens[k].text == "<":
            generics, k = _generic_names(tokens, k, end)
        params: tuple[Param, ...] = ()
        if k < end and tokens[k].text == "(":
            close = _match(tokens, k, end)
            params = _params(tokens[k + 1 : close])
            k = close + 1
        ret_start = k + 1 if k < end and tokens[k].text == "->" else None
        while k < end and tokens[k].text not in ("{", ";"):
            if tokens[k].text in ("(", "["):
                k = _match(tokens, k, end)
            k += 1
        if k >= end or tokens[k].text == ";":
            return k + 1  # a declaration without a body (trait method, extern fn)
        returns: tuple[str, ...] = ()
        if ret_start is not None:
            stop = next((m for m in range(ret_start, k) if tokens[m].text == "where"), k)
            returns = tuple(t.text for t in tokens[ret_start:stop])
        close = _match(tokens, k, end)
        qualname = "::".join((*scope.path, name))
        body_start = tokens[k + 1].line if k + 1 < close else tokens[close].line
        sig = " ".join(t.text for t in tokens[self._attr_start(i) : k])
        body = " ".join(t.text for t in tokens[k : close + 1])
        self.index.fns.append(
            RustFn(
                qualname=qualname,
                def_line=first_line,
                sig_line=sig_line,
                body_start=max(body_start, first_line),
                end_line=tokens[close].line,
                kind="method" if scope.kind in ("impl", "trait") else "function",
                params=params,
                returns=returns,
                generics=generics,
                is_test=is_test,
                self_type=scope.self_type,
                body_hash=_blake(body),
                sig_hash=_blake(sig),
            )
        )
        self.items(k + 1, close, _Scope((*scope.path, name), "fn", is_test, scope.self_type))
        return close + 1

    def _attr_start(self, i: int) -> int:
        """Index of the first outer attribute token directly before ``i`` (so ``sig_hash`` covers
        ``#[inline]`` and friends)."""
        k = i
        while k >= 2 and self.tokens[k - 1].text == "]":
            depth, m = 0, k - 1
            while m >= 0:
                if self.tokens[m].text == "]":
                    depth += 1
                elif self.tokens[m].text == "[":
                    depth -= 1
                    if depth == 0:
                        break
                m -= 1
            if m >= 1 and self.tokens[m - 1].text == "#":
                k = m - 1
            else:
                break
        return k

    def module(self, j: int, end: int, scope: _Scope, first_line: int, is_test: bool) -> int:
        tokens = self.tokens
        name = tokens[j + 1].text
        k = j + 2
        if k < end and tokens[k].text == "{":
            close = _match(tokens, k, end)
            self.items(k + 1, close, _Scope((*scope.path, name), "mod", is_test))
            return close + 1
        self.index.mods.append(ModDecl(scope.path, name, is_test))
        return k + 1

    def impl(self, j: int, end: int, scope: _Scope, first_line: int, is_test: bool) -> int:
        tokens = self.tokens
        k = j + 1
        if k < end and tokens[k].text == "<":
            k = _skip_angle(tokens, k, end)
        header: list[str] = []
        while k < end and tokens[k].text not in ("{", ";"):
            if tokens[k].text == "where":
                while k < end and tokens[k].text not in ("{", ";"):
                    k = _match(tokens, k, end) + 1 if tokens[k].text in ("(", "[") else k + 1
                break
            header.append(tokens[k].text)
            k += 1
        if k >= end or tokens[k].text == ";":
            return k + 1
        depth, split = 0, None
        for m, text in enumerate(header):
            depth += (text == "<") - (text == ">")
            if text == "for" and depth == 0 and m > 0:
                split = m
        if split is None:
            self_type = name = _type_name(header)
        else:
            trait = _type_name([t for t in header[:split] if t != "!"])
            self_type = _type_name(header[split + 1 :])
            name = f"<{self_type} as {trait}>"
        close = _match(tokens, k, end)
        self.items(k + 1, close, _Scope((*scope.path, name), "impl", is_test, self_type))
        return close + 1

    def trait(self, j: int, end: int, scope: _Scope, first_line: int, is_test: bool) -> int:
        tokens = self.tokens
        name = tokens[j + 1].text if j + 1 < end else "?"
        k = j + 2
        while k < end and tokens[k].text not in ("{", ";"):
            k = _match(tokens, k, end) + 1 if tokens[k].text in ("(", "[") else k + 1
        if k >= end or tokens[k].text == ";":
            return k + 1
        close = _match(tokens, k, end)
        self.items(k + 1, close, _Scope((*scope.path, name), "trait", is_test))
        return close + 1

    def enum(self, j: int, end: int, first_line: int, is_test: bool) -> int:
        tokens = self.tokens
        name = tokens[j + 1].text
        k = j + 2
        while k < end and tokens[k].text not in ("{", ";"):
            k = _match(tokens, k, end) + 1 if tokens[k].text in ("(", "[") else k + 1
        if k >= end or tokens[k].text == ";":
            return k + 1
        close = _match(tokens, k, end)
        members: list[str] = []
        for part in _split_top(tokens[k + 1 : close]):
            m = 0
            while m < len(part) and part[m].text == "#":  # #[default], #[serde(...)]
                m = _match(part, m + 1, len(part)) + 1 if m + 1 < len(part) else len(part)
            if m < len(part) and part[m].kind == "ident":
                members.append(part[m].text)
        if members and not is_test:
            self.index.enums[name] = tuple(members)
        return close + 1


def index_source(source: str) -> FileIndex:
    """Index one Rust file's text (see the module docstring)."""
    n_lines = source.count("\n") + (1 if source and not source.endswith("\n") else 0)
    return _Indexer(tokenize(source), n_lines).run()


def index_file(path: Path) -> FileIndex | None:
    """Index the regular file at ``path`` (None when it is missing, not a regular file, too large
    or not UTF-8)."""
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_SOURCE_BYTES:
            return None
        return index_source(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return None


# -- crates: module paths ---------------------------------------------------------------------------


@dataclass(slots=True)
class CrateIndex:
    """The files of one crate (a target's root file and every ``mod x;`` reachable from it).

    ``modules`` maps a root-relative path to its module path (``src/money.rs`` is ``("money",)``);
    :meth:`find` resolves a libtest test name (``money::tests::splits``) to the file and line of
    its function.
    """

    root: Path
    files: dict[str, FileIndex] = field(default_factory=dict)
    modules: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @classmethod
    def build(cls, root: Path, crate_root: Path) -> CrateIndex:
        """Index the crate whose root file is ``crate_root``; paths are relative to ``root``."""
        crate = cls(root)
        queue: list[tuple[Path, tuple[str, ...], bool, bool]] = [(crate_root, (), True, False)]
        while queue:
            path, module, owns_dir, test_only = queue.pop()
            rel = _relative(path, root)
            if rel is None or rel in crate.files:
                continue
            found = index_file(path)
            if found is None:
                continue
            if test_only:  # `#[cfg(test)] mod tests;`: the whole file is test code
                found.test_spans.append((1, max(found.n_lines, 1)))
                found.fns = [dataclasses.replace(fn, is_test=True) for fn in found.fns]
            crate.files[rel] = found
            crate.modules[rel] = module
            base = path.parent if owns_dir else path.parent / path.stem
            for decl in found.mods:
                directory = base.joinpath(*decl.scope)
                for candidate, owns in (
                    (directory / f"{decl.name}.rs", False),
                    (directory / decl.name / "mod.rs", True),
                ):
                    if candidate.is_file():
                        queue.append((candidate, (*module, *decl.scope, decl.name), owns, decl.is_test))
                        break
        return crate

    def find(self, test_name: str) -> tuple[str, int] | None:
        """``(path, def_line)`` of the function a libtest test name refers to, else None."""
        for rel, found in self.files.items():
            prefix = "::".join(self.modules[rel])
            for fn in found.fns:
                full = f"{prefix}::{fn.qualname}" if prefix else fn.qualname
                if full == test_name:
                    return rel, fn.def_line
        return None


def _relative(path: Path, root: Path) -> str | None:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return PurePosixPath(*rel.parts).as_posix()


# -- regions and type descriptors -------------------------------------------------------------------


def regions(path: str, found: FileIndex) -> list[FunctionRegion]:
    """The non-test functions of one file as neutral regions, ordinals in source order."""
    out: list[FunctionRegion] = []
    ordinals: dict[str, int] = {}
    for fn in sorted((f for f in found.fns if not f.is_test), key=lambda f: (f.def_line, f.sig_line)):
        ordinal = ordinals.get(fn.qualname, 0)
        ordinals[fn.qualname] = ordinal + 1
        out.append(
            FunctionRegion(
                func_key(path, fn.qualname, ordinal),
                path,
                fn.qualname,
                ordinal,
                fn.def_line,
                fn.body_start,
                fn.end_line,
                fn.body_hash,
                fn.sig_hash,
                tuple(p.name for p in fn.params),
                fn.kind,  # type: ignore[arg-type]
            )
        )
    return out


def type_descriptor(
    tokens: Sequence[str],
    *,
    generics: Collection[str] = (),
    enums: Mapping[str, Sequence[str]] | None = None,
    self_type: str | None = None,
) -> TypeDescriptor:
    """The neutral descriptor of a Rust type written as ``tokens``.

    Integers are ``int``, floats ``float``, ``str``/``String``/``char`` ``str``, ``()`` ``none``,
    ``Option<T>`` a union of T and ``none``, ``Result<T, E>`` a union of T and the error object,
    ``Vec<T>``/``[T]``/``[T; N]`` a list (of ``u8``: ``bytes``), sets and maps their kinds, tuples
    fixed tuples, smart pointers and references their target, an enum declared in the crate an
    ``enum`` with its variants, a type parameter or ``impl Trait``/``dyn Trait`` ``any``, and
    anything else an ``object`` named as written. ``Self`` is ``self_type`` (the ``impl`` block's
    type) when given, else ``any``.
    """
    written = [self_type if t == "Self" and self_type else t for t in tokens]
    return _descriptor(written, frozenset(generics), enums or {}, 0)


def _descriptor(
    tokens: list[str], generics: frozenset[str], enums: Mapping[str, Sequence[str]], depth: int
) -> TypeDescriptor:
    if depth > 16:
        return ANY
    while tokens and (tokens[0] in ("&", "&&", "mut", "*", "const") or tokens[0].startswith("'")):
        tokens = tokens[1:]
    if not tokens:
        return ANY
    head = tokens[0]
    if head in ("impl", "dyn", "_", "Self", "!"):
        return ANY
    if head == "(":
        inner = _split_strs(tokens[1:-1]) if tokens[-1] == ")" else []
        if not inner:
            return TypeDescriptor.scalar("none")
        if len(inner) == 1 and not (len(tokens) > 3 and tokens[-2] == ","):
            return _descriptor(inner[0], generics, enums, depth + 1)
        return TypeDescriptor.tuple_of([_descriptor(p, generics, enums, depth + 1) for p in inner])
    if head == "[":
        inner = tokens[1:-1] if tokens[-1] == "]" else tokens[1:]
        elem = _split_strs(inner, ";")[0] if inner else []
        if elem == ["u8"]:
            return TypeDescriptor.scalar("bytes")
        return TypeDescriptor.list_of(_descriptor(elem, generics, enums, depth + 1) if elem else None)
    name, args = _path_and_args(tokens)
    last = name.rsplit("::", 1)[-1]
    sub = [_descriptor(a, generics, enums, depth + 1) for a in args]
    if last in generics:
        return ANY
    if last in _INTS or last in _NONZERO:
        return TypeDescriptor.scalar("int")
    if last in ("f32", "f64"):
        return TypeDescriptor.scalar("float")
    if last == "bool":
        return TypeDescriptor.scalar("bool")
    if last in _STRS:
        return TypeDescriptor.scalar("str")
    if last == "Option" and sub:
        return TypeDescriptor.union(sub[0], TypeDescriptor.scalar("none"))
    if last == "Result" and sub:
        error = TypeDescriptor.object(_type_name(args[1])) if len(args) > 1 else ANY
        if len(args) > 1 and _type_name(args[1]).rsplit("::", 1)[-1] in enums:
            error = sub[1]
        return TypeDescriptor.union(sub[0], error)
    if last in _WRAPPERS and sub:
        return sub[0]
    if last in _LISTS:
        if args and args[0] == ["u8"]:
            return TypeDescriptor.scalar("bytes")
        return TypeDescriptor.list_of(sub[0] if sub else None)
    if last in _SETS:
        return TypeDescriptor.set_of(sub[0] if sub else None)
    if last in _MAPS:
        return TypeDescriptor.dict_of(sub[0] if sub else None, sub[1] if len(sub) > 1 else None)
    if last in enums and enums[last]:
        return TypeDescriptor.enum(last, tuple(enums[last])[:256])
    return TypeDescriptor.object(name[:512] or "?")


def _split_strs(tokens: Sequence[str], sep: str = ",") -> list[list[str]]:
    return [[t.text for t in part] for part in _split_top([Token("punct", t, 0) for t in tokens], sep)]


def _path_and_args(tokens: Sequence[str]) -> tuple[str, list[list[str]]]:
    """``std::collections::HashMap<K, V>`` → (``std::collections::HashMap``, [[K], [V]])."""
    for k, text in enumerate(tokens):
        if text == "<":
            close = next(
                (m for m in range(len(tokens) - 1, k, -1) if tokens[m] == ">"),
                len(tokens),
            )
            return "".join(tokens[:k]), _split_strs(tokens[k + 1 : close])
    return "".join(tokens), []


def descriptors(fn: RustFn, enums: Mapping[str, Sequence[str]]) -> dict[str, TypeDescriptor | None]:
    """Parameter name (and ``return``) → descriptor for one function."""
    out: dict[str, TypeDescriptor | None] = {}
    for param in fn.params:
        out[param.name] = (
            type_descriptor(param.type_tokens, generics=fn.generics, enums=enums, self_type=fn.self_type)
            if param.type_tokens
            else None
        )
    out["return"] = (
        type_descriptor(fn.returns, generics=fn.generics, enums=enums, self_type=fn.self_type)
        if fn.returns
        else TypeDescriptor.scalar("none")
    )
    return out


# -- the adapter ------------------------------------------------------------------------------------


class RustAdapter(NeutralAdapter):
    """:class:`~coverset.lang.base.LanguageAdapter` for Rust (Cargo) repositories.

    ``read_bundle`` reads the neutral bundle ``coverset rust run`` wrote (the upload path), or
    converts a raw capture directory (``capture.json`` with per-test LLVM exports) on the fly;
    ``index_functions`` and ``changed_functions`` read Rust sources with the indexer above, so a
    diff is classified per function without either side's bundle. ``env_spec`` raises: managed
    runs of a Rust suite are not built (see ``docs/rust.md``).
    """

    def __init__(
        self,
        *,
        functions: Iterable[FunctionRegion] = (),
        base_functions: Iterable[FunctionRegion] | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__("rust", functions=functions, base_functions=base_functions, **kwargs)  # type: ignore[arg-type]

    def detect(self, repo_root: Path) -> bool:
        """True when ``repo_root`` has a ``Cargo.toml`` (a package or a workspace)."""
        manifest = repo_root / "Cargo.toml"
        return manifest.is_file() and not manifest.is_symlink()

    def env_spec(self, repo_root: Path, cfg: Mapping) -> EnvSpec:
        raise NotRunnableError(
            "managed runs for Rust are not built: coverset cannot build a Rust test environment in "
            "its workers yet. Run `coverset rust run --crate PATH` locally or in your CI job and "
            "either open the report (`coverset report PATH/.coverset/neutral`) or upload the "
            "neutral bundle it writes (upload mode); see docs/rust.md"
        )

    def test_command(
        self, args_file: str, scores_file: str | None, out_dir: str, extra: Mapping
    ) -> list[str]:
        """The nextest invocation of a capture (``args_file`` and ``scores_file`` are not honoured:
        nextest orders and selects tests itself). The environment that makes it per-test (the
        instrumented ``RUSTFLAGS``, the profile-naming target runner) comes from
        :func:`coverset.lang.rust_llvm.capture_env`; ``extra["filterset"]`` narrows the run."""
        argv = [
            "cargo",
            "nextest",
            "run",
            "--profile",
            "coverset",
            "--tool-config-file",
            f"coverset:{Path(out_dir).resolve() / 'nextest.toml'}",
            "--no-fail-fast",
        ]
        filterset = extra.get("filterset") if isinstance(extra, Mapping) else None
        if isinstance(filterset, str) and filterset:
            argv += ["-E", filterset]
        return argv

    def read_bundle(
        self,
        bundle_dir: Path,
        *,
        limits: BundleLimits | None = None,
        source_root: Path | None = None,
        head_paths: Collection[str] | None = None,
    ) -> NeutralBundle:
        """A neutral bundle, or a raw capture converted first (then read under the same limits)."""
        import tempfile

        from coverset.lang.neutral import read_neutral_bundle
        from coverset.lang.rust_llvm import CAPTURE_FILE, CaptureError, convert_capture, write_neutral

        if (bundle_dir / CAPTURE_FILE).is_file():
            try:
                bundle = convert_capture(bundle_dir, root=source_root)
            except CaptureError as exc:
                raise BundleError(f"rust capture refused: {exc}") from exc
            with tempfile.TemporaryDirectory(prefix="coverset-rust-") as tmp:
                write_neutral(bundle, Path(tmp))
                return read_neutral_bundle(Path(tmp), limits=limits, head_paths=head_paths)
        return read_neutral_bundle(bundle_dir, limits=limits, head_paths=head_paths)

    def index_functions(self, path: str, source: str) -> list[FunctionRegion]:
        """The regions of one Rust file, from its source (test-only functions excluded)."""
        if not path.endswith(".rs"):
            return super().index_functions(path, source)
        return regions(path, index_source(source))

    def changed_functions(
        self, path: str, old_src: str | None, new_src: str | None, added: set[int], removed: set[int]
    ) -> list[FunctionChange]:
        """Classify the functions of one ``.rs`` file a diff touches, from both sides' sources."""
        if not path.endswith(".rs"):
            return super().changed_functions(path, old_src, new_src, added, removed)
        head = regions(path, index_source(new_src)) if new_src is not None else []
        base = regions(path, index_source(old_src)) if old_src is not None else []
        return NeutralAdapter("rust", functions=head, base_functions=base).changed_functions(
            path, old_src, new_src, added, removed
        )


def iter_rust_files(root: Path) -> Iterator[Path]:
    """Every ``.rs`` file under ``root`` outside ``target/`` and hidden directories."""
    for path in sorted(root.rglob("*.rs")):
        parts = path.relative_to(root).parts
        if any(p == "target" or p.startswith(".") for p in parts[:-1]):
            continue
        yield path
