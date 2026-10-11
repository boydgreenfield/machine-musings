"""Safe GitHub-flavoured markdown building blocks for text coverset does not control.

Test ids, failure messages, qualnames and reprs come from the PR's own code, so every interpolated
string goes through :func:`md_text` (escaped inline text), :func:`code_span` or :func:`code_block`.
None of them can produce raw HTML, images, links, @-mentions, issue or ``GH-`` references, or a
forged sticky-comment marker. Bidirectional and other invisible format characters are removed
everywhere (they could make the comment read differently from what it says).
"""

from __future__ import annotations

import re

MARKDOWN_SPECIALS = frozenset("\\`*_{}[]()#+-.!|<>~=:\"'$^")
"""ASCII punctuation that is backslash-escaped in inline text (CommonMark allows any of it)."""

ZERO_WIDTH_SPACE = "\u200b"
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u2028\u2029]")
_FORMAT = re.compile(
    r"[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb]"
)
"""Bidi embeddings/overrides/isolates, directional marks, zero-width and other invisible format characters."""
_GH_REFERENCE = re.compile(r"(?i)\b(gh)(\\?-)")
_WHITESPACE_RUN = re.compile(r"\s+")
_BACKTICK_RUN = re.compile(r"`+")
_COMMENT_OPEN = "<!--"

DEFAULT_INLINE_LIMIT = 300


def strip_format_chars(text: str) -> str:
    """``text`` without bidi controls and invisible format characters."""
    return _FORMAT.sub("", text)


def _clean(text: str, limit: int) -> str:
    text = _CONTROL.sub(" ", strip_format_chars(str(text)))
    text = _WHITESPACE_RUN.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def neutralise_mentions(text: str) -> str:
    """Put a zero-width space after every ``@`` so ``@user`` and ``@org/team`` never notify."""
    return text.replace("@", "@" + ZERO_WIDTH_SPACE)


def neutralise_references(text: str) -> str:
    """Break GitHub's ``#12``, ``owner/repo#12`` and ``GH-12`` autolinks with a zero-width space.

    Backslash-escaping ``#`` is not enough: GitHub links references after rendering, and a
    cross-repository reference would leave a "mentioned this" backlink in someone else's repo.
    """
    text = text.replace("#", "#" + ZERO_WIDTH_SPACE)
    return _GH_REFERENCE.sub(lambda m: m.group(1) + ZERO_WIDTH_SPACE + m.group(2), text)


def md_text(text: object, *, limit: int = DEFAULT_INLINE_LIMIT) -> str:
    """Escape ``text`` for inline markdown: one line, specials backslash-escaped, ``&`` as an
    entity, mentions and issue references neutralised, so it renders literally (no HTML, links,
    emphasis, tables, notifications or backlinks)."""
    cleaned = _clean(str(text), limit)
    out = []
    for char in cleaned:
        if char == "&":
            out.append("&amp;")
        elif char in MARKDOWN_SPECIALS:
            out.append("\\" + char)
        else:
            out.append(char)
    return neutralise_references(neutralise_mentions("".join(out)))


def _longest_backtick_run(text: str) -> int:
    return max((len(m.group()) for m in _BACKTICK_RUN.finditer(text)), default=0)


def code_span(text: object, *, in_table: bool = False, limit: int = DEFAULT_INLINE_LIMIT) -> str:
    """An inline code span whose fence is longer than any backtick run inside ``text``.

    In a GFM table cell ``|`` must still be escaped inside code spans, so ``in_table`` does that.
    A literal ``<!--`` is broken with a zero-width space so the span can never carry the marker.
    """
    body = _clean(str(text), limit).replace(_COMMENT_OPEN, "<!" + ZERO_WIDTH_SPACE + "--")
    if in_table:
        body = body.replace("|", "\\|")
    if not body:
        return "` `"
    fence = "`" * (_longest_backtick_run(body) + 1)
    if body.startswith("`") or body.endswith("`") or (body.startswith(" ") and body.endswith(" ")):
        body = f" {body} "
    return f"{fence}{body}{fence}"


def code_block(text: str, language: str = "") -> str:
    """A fenced code block whose backtick fence (at least 3) is longer than any run inside."""
    body = _CONTROL.sub(" ", strip_format_chars(text).replace("\r\n", "\n").replace("\t", "    "))
    body = body.rstrip("\n")
    body = body.replace(_COMMENT_OPEN, "<!" + ZERO_WIDTH_SPACE + "--")
    fence = "`" * max(3, _longest_backtick_run(body) + 1)
    info = language if re.fullmatch(r"[A-Za-z0-9_+-]{0,20}", language) else ""
    return f"{fence}{info}\n{body}\n{fence}"


def safe_url(url: str | None) -> str | None:
    """``url`` when it is a plain absolute http(s) URL without spaces, quotes or brackets, else None."""
    if not url or not re.fullmatch(r"https?://[^\s<>\"'()\[\]`]+", url):
        return None
    return url
