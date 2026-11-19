# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Mastodon status text: HTML content to plain text, and length as Mastodon counts it."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from wingman.platforms.bluesky.richtext import grapheme_length

URL = re.compile(r"https?://[^\s<>\"]+")
MENTION = re.compile(r"(?<![\w/])@(\w+)@[\w.-]+\w")
URL_LENGTH = 23  # every link counts as 23 characters, however long it is


class _Text(HTMLParser):
    """Mastodon shortens the visible text of long links (hidden spans + ellipsis), so a plain link is
    replaced by its full href; mentions and hashtags keep their visible text (@bob, #java)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "mention" not in (a.get("class") or "").split() and a.get("href"):
            self.parts.append(a["href"])
            self._in_link = True
        elif tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_link = False
        elif tag == "p":
            self.parts.append("\n\n")

    def handle_data(self, data):
        if not self._in_link:
            self.parts.append(data)


def html_to_text(content: str) -> str:
    """Status HTML -> plain text: paragraphs and line breaks kept, links shown in full."""
    p = _Text()
    p.feed(content)
    return re.sub(r"\n{3,}", "\n\n", "".join(p.parts)).strip()


def status_length(text: str) -> int:
    """Characters as Mastodon counts them: links are 23, `@user@domain` counts as `@user`."""
    text = URL.sub("x" * URL_LENGTH, text)
    text = MENTION.sub(lambda m: f"@{m.group(1)}", text)
    return grapheme_length(text)
