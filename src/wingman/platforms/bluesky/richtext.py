# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Bluesky post text helpers: URLs only become clickable links when declared as 'facets'."""

from __future__ import annotations

import re
import unicodedata

URL = re.compile(r"https?://[^\s<>\"]+")
TRAILING_PUNCTUATION = ".,;:!?)]}'\""


def link_facets(text: str) -> list[dict]:
    """Link facets for every URL in `text`. Offsets are UTF-8 byte positions, as the protocol requires."""
    facets = []
    for m in URL.finditer(text):
        url = m.group(0).rstrip(TRAILING_PUNCTUATION)
        start = len(text[:m.start()].encode())
        facets.append({
            "index": {"byteStart": start, "byteEnd": start + len(url.encode())},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
        })
    return facets


def grapheme_length(text: str) -> int:
    """Approximate user-perceived characters (Bluesky's 300 limit counts graphemes, not bytes).

    Combining marks and zero-width joiners don't start a new grapheme. Not a full UAX #29
    implementation, but it never undercounts plain text and handles accents and most emoji.
    """
    count, joined = 0, False
    for ch in text:
        if unicodedata.combining(ch) or ch in "‍️" or 0x1F3FB <= ord(ch) <= 0x1F3FF:
            joined = ch == "‍"
            continue
        if joined:
            joined = False
            continue
        count += 1
    return count
