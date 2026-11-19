# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Platform-neutral data model. Every adapter converts its native objects into these."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal


def now_utc() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Constraints:
    max_length: int
    markdown: bool = True
    ai_drafts_allowed: bool = True  # False -> discovery only (e.g. Stack Overflow)
    notes: str = ""  # platform etiquette passed to the drafter
    length: Callable[[str], int] = len  # how the platform counts characters (Bluesky: graphemes)


@dataclass
class Comment:
    author: str
    body: str
    score: int


@dataclass
class Thread:
    platform: str
    id: str  # platform-native id, unique per platform
    source: str  # subreddit / feed / tag
    title: str
    body: str
    url: str
    author: str
    score: int
    num_comments: int
    created_at: datetime
    top_comments: list[Comment] = field(default_factory=list)
    source_rules: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.platform}:{self.id}"

    def age_hours(self, now: datetime) -> float:
        return max((now - self.created_at).total_seconds() / 3600, 0.01)


@dataclass
class RankedThread:
    """A thread plus how well it suits you (from `wingman scan`)."""
    thread: Thread
    relevance: float  # 0-10, Claude's judgement of your fit
    opportunity: float  # 0-1, freshness / traction / room
    final: float  # combined 0-1
    angle: str = ""


@dataclass(frozen=True)
class LinkCard:
    """Preview card for a link in a new post (title + description, as social apps show them)."""
    uri: str
    title: str
    description: str = ""


@dataclass
class Reply:
    platform: str
    thread_id: str
    source: str
    text: str


@dataclass
class ActivityItem:
    platform: str
    id: str
    source: str
    body: str
    score: int
    created_at: datetime
    permalink: str = ""


DocumentKind = Literal["blog", "github", "project", "page", "reply", "file"]


@dataclass
class Document:
    """A piece of your own writing or background (blog post, README, page, file, reply)."""
    title: str
    text: str
    kind: DocumentKind
    url: str = ""
    date: str = ""  # ISO date written / last updated; "" = unknown or evergreen
