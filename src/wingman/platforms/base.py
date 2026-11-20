# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""The contract every platform adapter implements."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from wingman.core.models import ActivityItem, Constraints, LinkCard, Reply, Thread

Posting = Literal["api", "manual"]  # api: submit() posts; manual: you post from the browser


class Platform(Protocol):
    name: str
    constraints: Constraints
    posting: Posting
    has_scores: bool  # False when the data source carries no vote counts

    def discover(self, sources: list[str], since: datetime, limit: int) -> list[Thread]:
        """Candidate threads from the given sources created after `since`."""
        ...

    def fetch_context(self, thread: Thread) -> Thread:
        """Fill in top comments and source rules for a thread."""
        ...

    def thread_from_url(self, url: str) -> Thread | None:
        """Load a thread (with replies) from its web URL, or None if this adapter can't (paste it instead)."""
        ...

    def submit(self, reply: Reply) -> str:
        """Post a reply. Only ever called after the user approves it. Returns a URL."""
        ...

    def publish(self, text: str, link: LinkCard | None = None) -> str:
        """Create a new standalone post (not a reply). Only after the user approves it. Returns a URL."""
        ...

    # Optional, for posting = "manual" platforms that take new posts: the web page to open for one
    # (`wingman post` copies the text and opens it). Absent -> new posts need posting = "api".
    # def new_post_url(self, title: str, link: LinkCard | None = None) -> str: ...

    def my_activity(self, limit: int) -> list[ActivityItem]:
        """The user's own recent comments with their scores."""
        ...
