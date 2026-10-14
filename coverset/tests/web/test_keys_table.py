"""The keys documented are the keys bound: the help dialogs, docs/ui.md and docs/report.md agree
with the ``KEYS`` tables of ``app.js`` and ``report.js``, and the two scripts bind the same keys
(less the pages the static report does not have).

A key is a single key (``j``, ``Escape``, ``.``) or a go-to chord written ``g-o``. The dialogs mark
each row with ``data-keys`` (space-separated) and ``data-scope``; the docs tables list keys in
backticks in their first column (``g o`` is a chord, ``Esc`` is Escape).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WEB_JS = ROOT / "src/coverset/web/static/app.js"
REPORT_JS = ROOT / "src/coverset/report/static/report.js"
WEB_LAYOUT = ROOT / "src/coverset/web/templates/layout.html"
REPORT_LAYOUT = ROOT / "src/coverset/report/templates/layout.html"
WEB_ONLY = {"r", "g-e", "g-i"}
"""The run page, the economy page and the invariants page exist only on the live pages."""


def bound(script: Path) -> dict[str, str]:
    """``key -> scope`` from a script's ``KEYS`` table, plus ``g-<letter>`` (scope ``go``) for each
    of its ``GO_KEYS``; ``g`` itself only starts a chord."""
    text = script.read_text()
    table = re.search(r"var KEYS = \{\n(.*?)\n  \};", text, re.S)
    assert table, f"no KEYS table in {script.name}"
    keys = dict(re.findall(r'^\s+"([^"]+)": \{ scope: "(\w+)"', table.group(1), re.M))
    assert keys.pop("g") == "go"
    go = re.search(r"var GO_KEYS = \[([^\]]*)\]", text)
    assert go, f"no GO_KEYS in {script.name}"
    for letter in re.findall(r'"(\w)"', go.group(1)):
        keys[f"g-{letter}"] = "go"
    return keys


def dialog(layout: Path) -> dict[str, str]:
    """``key -> scope`` of the rows of a layout's keys dialog."""
    text = layout.read_text()
    body = text[text.index('<dialog id="keys-help"') : text.index("</dialog>")]
    out: dict[str, str] = {}
    for scope, keys in re.findall(r'<tr data-scope="(\w+)"(?: data-go-row="\w")? data-keys="([^"]+)"', body):
        for key in keys.split():
            assert key not in out, f"{key} listed twice in {layout}"
            out[key] = scope
    assert len(re.findall(r"<tr ", body)) == len(re.findall(r"<tr data-scope=", body)), "a row without keys"
    return out


def documented(doc: Path, heading: str = "## Keys") -> set[str]:
    """The keys in the first column of the table under ``heading`` in ``doc``."""
    text = doc.read_text()
    section = text[text.index(heading + "\n") :]
    section = section[: section.find("\n## ", 1)]
    keys: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("| `"):
            continue
        for token in re.findall(r"`([^`]+)`", line.split("|")[1]):
            token = "Escape" if token == "Esc" else token
            keys.add(token.replace(" ", "-") if re.fullmatch(r"g \w", token) else token)
    return keys


@pytest.mark.parametrize(("script", "layout"), [(WEB_JS, WEB_LAYOUT), (REPORT_JS, REPORT_LAYOUT)])
def test_the_help_dialog_lists_exactly_the_bound_keys_with_their_scope(script: Path, layout: Path) -> None:
    keys = bound(script)
    rows = dialog(layout)
    assert rows == keys


@pytest.mark.parametrize(
    ("script", "doc"), [(WEB_JS, ROOT / "docs/ui.md"), (REPORT_JS, ROOT / "docs/report.md")]
)
def test_the_docs_list_exactly_the_bound_keys(script: Path, doc: Path) -> None:
    assert documented(doc) == set(bound(script))


def test_the_report_binds_the_live_pages_keys() -> None:
    web, report = bound(WEB_JS), bound(REPORT_JS)
    assert set(web) - set(report) == WEB_ONLY
    assert set(report) <= set(web)
    assert {k: web[k] for k in report} == report, "a shared key applies on the same kind of page"


def test_every_scope_has_a_test_for_its_page() -> None:
    for script in (WEB_JS, REPORT_JS):
        text = script.read_text()
        scopes = set(re.findall(r"^\s+(\w+): function \(\) \{ return", text, re.M))
        assert set(bound(script).values()) <= scopes, script.name


def test_the_keys_are_ignored_while_typing_and_with_modifiers() -> None:
    for script in (WEB_JS, REPORT_JS):
        text = script.read_text()
        assert "event.metaKey || event.ctrlKey || event.altKey" in text
        assert "if (typing(event)) return;" in text
