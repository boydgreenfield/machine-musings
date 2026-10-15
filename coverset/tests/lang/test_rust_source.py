"""The Rust source indexer (tokens, items, test-only code, module paths, descriptors) and the adapter."""

from __future__ import annotations

from pathlib import Path

import pytest

from coverset.lang import adapter_for_language
from coverset.lang.base import LanguageAdapter
from coverset.lang.neutral import NEUTRAL_LANGUAGES, NotRunnableError
from coverset.lang.rust import CrateIndex, RustAdapter, index_source, regions, tokenize, type_descriptor
from coverset_model.types import to_json

ROOT = Path(__file__).resolve().parents[2]
CRATE = ROOT / "examples" / "rust-app"

TRICKY = r"""
//! Inner doc comment with a brace {
/* a /* nested */ block comment with } braces */
use std::fmt::{self, Display};

const BRACES: &str = "}}}{";
static RAW: &str = r#"fn not_a_function() { "quoted" }"#;
const CH: char = '{';
const BYTES: &[u8] = br"{";

pub(crate) const fn constant() -> u8 {
    b'}'
}

pub async unsafe fn tricky<'a, T: Display + 'a, const N: usize>(
    label: &'a str,
    mut items: [T; N],
    (left, right): (i32, i32),
) -> Option<&'a str>
where
    T: Clone,
{
    'outer: loop {
        let c = '\'';
        if c == '}' { break 'outer; }
    }
    fn helper(x: i64) -> i64 {
        x + 1
    }
    let _ = |y: u8| { y };
    Some(label)
}

pub trait Shape {
    fn area(&self) -> f64;
    fn describe(&self) -> String {
        format!("{}", self.area())
    }
}

pub struct Square<T>(T);

impl<T: Into<f64> + Copy> Shape for Square<T> {
    #[inline]
    fn area(&self) -> f64 {
        let s: f64 = self.0.into();
        s * s
    }
}

impl<T> Square<T> {
    pub fn new(side: T) -> Self {
        Square(side)
    }
}

macro_rules! make_fn {
    ($name:ident) => {
        fn $name() {}
    };
}

extern "C" {
    fn abs(x: i32) -> i32;
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn works() {
        assert_eq!(constant(), b'}');
    }
}

#[tokio::test]
async fn an_async_test() {}

#[cfg(not(test))]
fn only_in_release() {}
"""


def test_tokens_skip_comments_strings_and_chars() -> None:
    texts = [t.text for t in tokenize(TRICKY)]
    assert "not_a_function" not in texts and "nested" not in texts
    assert texts.count("{") == texts.count("}")
    lifetimes = {t.text for t in tokenize(TRICKY) if t.kind == "life"}
    assert lifetimes == {"'a", "'outer"}
    assert [t.text for t in tokenize("x..=y 1.5 a.0 r#type ->")] == [
        "x",
        "..=",
        "y",
        "1.5",
        "a",
        ".",
        "0",
        "type",
        "->",
    ]
    assert [t.line for t in tokenize('a\n"two\nlines"\nb')] == [1, 2, 4]
    assert [t.kind for t in tokenize("'unterminated \"open string")] == ["life", "lit"]  # no raise
    assert tokenize("x /* never closed") == tokenize("x")


def test_items_regions_and_test_code() -> None:
    found = index_source(TRICKY)
    by_name = {f.qualname: f for f in found.fns}
    assert set(by_name) == {
        "constant",
        "tricky",
        "tricky::helper",
        "Shape::describe",
        "<Square as Shape>::area",
        "Square::new",
        "tests::works",
        "an_async_test",
        "only_in_release",
    }
    tricky = by_name["tricky"]
    assert [p.name for p in tricky.params] == ["label", "items", "(left,right)"]
    assert tricky.generics == ("T", "N") and tricky.kind == "function"
    lines = TRICKY.splitlines()
    assert lines[tricky.sig_line - 1].startswith("pub async unsafe fn tricky")
    assert lines[tricky.end_line - 1] == "}" and lines[tricky.body_start - 1].strip() == "'outer: loop {"
    area = by_name["<Square as Shape>::area"]
    assert area.kind == "method" and lines[area.def_line - 1].strip() == "#[inline]"
    assert area.sig_line == area.def_line + 1
    assert by_name["tests::works"].is_test and by_name["an_async_test"].is_test
    assert not by_name["only_in_release"].is_test and not by_name["constant"].is_test
    excluded = found.excluded_lines()
    module_start = next(i for i, line in enumerate(lines, 1) if line == "#[cfg(test)]")
    assert module_start in excluded and by_name["tests::works"].end_line in excluded
    assert by_name["tricky"].sig_line not in excluded
    names = [r.qualname for r in regions("src/lib.rs", found)]
    assert "tests::works" not in names and "an_async_test" not in names and "constant" in names


def test_hashes_ignore_formatting_and_comments() -> None:
    a = index_source("fn f(x: i32) -> i32 {\n    x + 1 // one\n}\n").fns[0]
    b = index_source("// moved\n\nfn f(x: i32)\n    -> i32\n{\n    /* plus */ x\n        + 1\n}\n").fns[0]
    c = index_source("fn f(x: i32) -> i32 {\n    x + 2\n}\n").fns[0]
    d = index_source("#[inline]\nfn f(x: i32) -> i32 {\n    x + 1\n}\n").fns[0]
    assert (a.body_hash, a.sig_hash) == (b.body_hash, b.sig_hash)
    assert a.body_hash != c.body_hash and a.sig_hash == c.sig_hash
    assert a.body_hash == d.body_hash and a.sig_hash != d.sig_hash


def test_ordinals_for_same_named_functions() -> None:
    source = "impl A<u8> { fn get(&self) {} }\nimpl A<u16> { fn get(&self) {} }\n"
    found = regions("src/a.rs", index_source(source))
    assert [(r.qualname, r.ordinal) for r in found] == [("A::get", 0), ("A::get", 1)]
    assert found[0].func_key != found[1].func_key


def test_crate_modules_resolve_test_names(tmp_path: Path) -> None:
    crate = CrateIndex.build(CRATE, CRATE / "src" / "lib.rs")
    assert crate.modules["src/money.rs"] == ("money",)
    assert crate.find("money::tests::refuses_to_overdraw") == ("src/money.rs", 115)
    assert crate.find("money::nothing") is None
    (tmp_path / "src" / "net").mkdir(parents=True)
    (tmp_path / "src" / "lib.rs").write_text("pub mod net;\n#[cfg(test)]\nmod tests;\n")
    (tmp_path / "src" / "net.rs").write_text("pub mod http;\npub fn up() {}\n")
    (tmp_path / "src" / "net" / "http.rs").write_text("pub fn get() {}\n#[test]\nfn gets() {}\n")
    (tmp_path / "src" / "tests.rs").write_text("#[test]\nfn whole_file() {}\nfn helper() {}\n")
    nested = CrateIndex.build(tmp_path, tmp_path / "src" / "lib.rs")
    assert nested.modules["src/net/http.rs"] == ("net", "http")
    assert nested.find("net::http::gets") == ("src/net/http.rs", 2)
    tests_file = nested.files["src/tests.rs"]
    assert all(f.is_test for f in tests_file.fns) and 3 in tests_file.excluded_lines()


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("i64", {"kind": "int"}),
        ("&'a mut u8", {"kind": "int"}),
        ("f32", {"kind": "float"}),
        ("&str", {"kind": "str"}),
        ("String", {"kind": "str"}),
        ("bool", {"kind": "bool"}),
        ("()", {"kind": "none"}),
        ("&[u8]", {"kind": "bytes"}),
        ("Vec<u8>", {"kind": "bytes"}),
        ("&[u32]", {"kind": "list", "elem": {"kind": "int"}}),
        ("[f64; 3]", {"kind": "list", "elem": {"kind": "float"}}),
        ("Option<String>", {"kind": "union", "arms": [{"kind": "str"}, {"kind": "none"}]}),
        ("(u32, u32)", {"kind": "tuple", "elems": [{"kind": "int"}, {"kind": "int"}]}),
        ("(u32,)", {"kind": "tuple", "elems": [{"kind": "int"}]}),
        ("HashMap<String, Vec<i32>>", {"kind": "dict", "key": {"kind": "str"}, "value": {"kind": "list", "elem": {"kind": "int"}}}),
        ("BTreeSet<char>", {"kind": "set", "elem": {"kind": "str"}}),
        ("Box<dyn Fn(i32) -> i32>", {"kind": "any"}),
        ("Arc<Mutex<u64>>", {"kind": "int"}),
        ("impl Iterator<Item = u8>", {"kind": "any"}),
        ("T", {"kind": "any"}),
        ("Role", {"kind": "enum", "name": "Role", "members": ["Admin", "Member"]}),
        ("crate::money::Ledger", {"kind": "object", "name": "crate::money::Ledger"}),
        ("Result<u64, Role>", {"kind": "union", "arms": [{"kind": "int"}, {"kind": "enum", "name": "Role", "members": ["Admin", "Member"]}]}),
        ("Result<(), io::Error>", {"kind": "union", "arms": [{"kind": "none"}, {"kind": "object", "name": "io::Error"}]}),
        ("Self", {"kind": "object", "name": "Ledger"}),
    ],
)  # fmt: skip
def test_type_descriptors(written: str, expected: dict) -> None:  # type: ignore[type-arg]
    tokens = [t.text for t in tokenize(written)]
    found = type_descriptor(tokens, generics=("T",), enums={"Role": ("Admin", "Member")}, self_type="Ledger")
    assert to_json(found) == expected


def test_the_adapter() -> None:
    adapter = adapter_for_language("rust")
    assert isinstance(adapter, RustAdapter) and isinstance(adapter, LanguageAdapter)
    assert adapter.name == "rust" and "rust" in NEUTRAL_LANGUAGES
    assert adapter.detect(CRATE) and not adapter.detect(ROOT / "docs")
    with pytest.raises(NotRunnableError, match="managed runs for Rust are not built"):
        adapter.env_spec(CRATE, {})
    argv = adapter.test_command("args", None, "/w", {"filterset": "test(=a)"})
    assert argv[:3] == ["cargo", "nextest", "run"] and argv[-2:] == ["-E", "test(=a)"]
    assert "coverset:/w/nextest.toml" in argv[argv.index("--tool-config-file") + 1]
    source = (CRATE / "src" / "money.rs").read_text()
    names = [r.qualname for r in adapter.index_functions("src/money.rs", source)]
    assert names == [
        "allocate_cents",
        "format_cents",
        "Ledger::new",
        "Ledger::post",
        "Ledger::balance",
        "Ledger::transfer",
    ]


def test_changed_functions_from_sources() -> None:
    adapter = RustAdapter()
    old = "fn a() -> i32 {\n    1\n}\n\nfn b() {}\n\nfn gone() {}\n"
    new = "fn a() -> i32 {\n    2\n}\n\n// a comment\nfn b() {}\n\nfn fresh() {}\n"
    changes = adapter.changed_functions("src/x.rs", old, new, added={2, 5, 8}, removed={2, 7})
    kinds = {(c.region or c.prev).qualname: c.kind for c in changes}  # type: ignore[union-attr]
    assert kinds == {"a": "modified", "fresh": "added", "gone": "deleted"}
    reformatted = "fn a() -> i32 { 1 }\n\nfn b() {}\n\nfn gone() {}\n"
    trivial = adapter.changed_functions("src/x.rs", old, reformatted, added={1}, removed={1, 2, 3})
    assert [(c.region.qualname, c.kind) for c in trivial if c.region] == [("a", "trivial")]
    assert adapter.changed_functions("src/x.rs", None, "fn n() {}\n", {1}, set())[0].kind == "added"
