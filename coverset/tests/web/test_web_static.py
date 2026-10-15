"""Vendored scripts match their pinned SRI hashes; the stylesheet keeps the theming contract."""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import coverset.web

STATIC = Path(coverset.web.__file__).parent / "static"


def sri(path: Path) -> str:
    return "sha384-" + base64.b64encode(hashlib.sha384(path.read_bytes()).digest()).decode()


def test_vendored_files_match_their_recorded_hashes() -> None:
    readme = (STATIC / "vendor" / "README.md").read_text()
    rows = re.findall(r"\| `([^`]+)` \| (https://\S+) \| `(sha384-[^`]+)` \| (\d+) \|", readme)
    assert {name for name, *_ in rows} == {"htmx.min.js", "sse.min.js", "idiomorph-ext.min.js"}
    for name, url, digest, size in rows:
        path = STATIC / "vendor" / name
        assert sri(path) == digest, name
        assert path.stat().st_size == int(size), name
        assert re.search(r"@\d+\.\d+\.\d+/", url), "pinned version"


def test_stylesheet_theming_contract() -> None:
    css = (STATIC / "app.css").read_text()
    assert "color-scheme: light dark" in css
    assert ':root[data-theme="dark"]' in css and ':root[data-theme="light"]' in css
    assert "light-dark(" in css and "tabular-nums" in css and "@media print" in css
    for okabe_ito in ("#0072b2", "#d55e00", "#f0e442"):
        assert okabe_ito in css.lower()
    assert ":focus-visible" in css


def test_app_js_has_no_eval() -> None:
    script = (STATIC / "app.js").read_text() + (STATIC / "keys.js").read_text()
    assert "eval(" not in script and "new Function" not in script and "innerHTML" not in script


def test_markup_is_built_only_in_render_modules() -> None:
    web = STATIC.parent
    for template in (web / "templates").rglob("*.html"):
        text = template.read_text()
        assert "|safe" not in text.replace(" ", ""), template
        assert "style=" not in text, template
    for module in web.glob("*.py"):
        assert "Markup(" not in module.read_text(), module
