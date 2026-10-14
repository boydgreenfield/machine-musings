"""GitHub-flavoured markdown for the outbox, AI findings and invariant statements.

markdown-it-py with ``html=False`` (raw HTML is shown as text), tables and strikethrough on, images
off, and links only to absolute http(s) URLs (every other scheme and every relative link is left as
plain text). The sticky comment's one piece of raw HTML, its fixed ``<details><summary>`` skeleton,
is recognised only as whole lines and rebuilt as real elements around separately rendered markdown;
the hidden ``<!-- coverset:sticky -->`` marker line is dropped.
"""

from __future__ import annotations

import re

from markdown_it import MarkdownIt
from markupsafe import Markup

__all__ = ["render_markdown", "render_sticky"]

_DETAILS_OPEN = re.compile(r"^<details><summary>(.*)</summary>\s*$")
_DETAILS_CLOSE = re.compile(r"^</details>\s*$")
_MARKER = re.compile(r"^<!--\s*coverset:sticky\s*-->\s*$")
_LINK = re.compile(r"^https?://[^\s<>\"'`]+$", re.IGNORECASE)


def _parser() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False, "linkify": False, "typographer": False})
    md.enable(["table", "strikethrough"])
    md.disable("image")
    md.validateLink = lambda url: bool(_LINK.match(url))  # type: ignore[method-assign]
    return md


_MD = _parser()


def render_markdown(text: str | None) -> Markup:
    """``text`` as HTML (block level); never contains raw HTML from the input."""
    if not text:
        return Markup("")
    return Markup(_MD.render(text))


def render_inline(text: str | None) -> Markup:
    """``text`` as inline HTML (no paragraph wrapper)."""
    if not text:
        return Markup("")
    return Markup(_MD.renderInline(text))


def render_sticky(text: str | None) -> Markup:
    """A sticky comment or check summary: markdown plus its ``<details>`` skeleton, marker removed."""
    if not text:
        return Markup("")
    out: list[Markup] = []
    chunk: list[str] = []
    depth = 0

    def flush() -> None:
        if chunk:
            out.append(render_markdown("\n".join(chunk)))
            chunk.clear()

    for line in text.splitlines():
        if _MARKER.match(line):
            continue
        if match := _DETAILS_OPEN.match(line):
            flush()
            out.append(Markup("<details><summary>") + render_inline(match.group(1)) + Markup("</summary>"))
            depth += 1
            continue
        if _DETAILS_CLOSE.match(line) and depth:
            flush()
            out.append(Markup("</details>"))
            depth -= 1
            continue
        chunk.append(line)
    flush()
    out.extend(Markup("</details>") for _ in range(depth))
    return Markup("").join(out)
