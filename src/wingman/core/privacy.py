"""Private terms (employers, clients, colleagues): kept from the drafting model and blocked at posting."""

from __future__ import annotations

import re

PLACEHOLDER = "[employer]"


def _pattern(term: str) -> re.Pattern[str]:
    # Whole-word, case-insensitive; works for terms with spaces or punctuation.
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.I)


def redact(text: str, terms: list[str], placeholder: str = PLACEHOLDER) -> str:
    for term in terms:
        text = _pattern(term).sub(placeholder, text)
    return text


def find(text: str, terms: list[str]) -> list[str]:
    """The private terms that appear in `text`."""
    return [term for term in terms if _pattern(term).search(text)]
