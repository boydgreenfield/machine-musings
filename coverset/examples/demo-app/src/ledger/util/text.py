"""Small string helpers shared across the ledger."""

import re
import unicodedata

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")


def slugify(text: str) -> str:
    """Return a lowercase ASCII slug: ``"Acme Corp."`` becomes ``"acme-corp"``."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", ascii_text.lower()).strip("-")


def normalize_whitespace(text: str) -> str:
    """Collapse runs of whitespace to a single space and trim both ends."""
    return _WHITESPACE.sub(" ", text).strip()


def truncate(text: str, width: int, placeholder: str = "...") -> str:
    """Shorten ``text`` to at most ``width`` characters, ending with ``placeholder`` when cut."""
    if len(text) <= width:
        return text
    if width <= len(placeholder):
        return placeholder[:width]
    return text[: width - len(placeholder)].rstrip() + placeholder


def initials(name: str) -> str:
    """Return the uppercase first letter of each word in ``name``."""
    return "".join(word[0].upper() for word in name.split())
