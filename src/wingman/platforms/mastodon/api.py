# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Mastodon adapter: hashtag/search/home discovery, thread context, replies and your post history.

Everything goes through your own instance, so posts from other servers carry your instance's ids and
replies federate back to their authors as usual.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from wingman.core.models import ActivityItem, Comment, Constraints, LinkCard, Reply, Thread
from wingman.platforms.mastodon.client import MastodonClient
from wingman.platforms.mastodon.text import html_to_text, status_length

log = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 500
MENTION_ROOM = 32  # "@username " is added in front of replies; usernames are at most 30 characters
PAGE = 40  # Mastodon's maximum page size
TOP_REPLIES = 8
# A status page on any instance: https://host/@user/123 or https://host/@user@other.host/123
WEB_STATUS_URL = re.compile(r"^https://[^/\s]+/@[\w.@-]+/\d+/?$")

NOTES = ("Mastodon: conversational, plain text (markdown shows as literal symbols). Links count as 23 "
         "characters. People are wary of anything promotional or bot-like: answer the actual post, and only "
         "link your own writing when it directly helps. At most one or two hashtags, and only when they add "
         "reach; never in a reply. The author's @mention is added automatically, so don't include it.")


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MastodonPlatform:
    name = "mastodon"
    posting = "api"
    has_scores = True  # favourites

    def __init__(self, settings: dict, client: MastodonClient | None = None):
        self.settings = settings
        self.client = client or MastodonClient(settings.get("instance", ""),
                                               os.environ.get("MASTODON_ACCESS_TOKEN", ""))
        self.languages = set(settings.get("languages", ["en"]))
        self.visibility = settings.get("visibility", "public")
        self.include_bots = settings.get("include_bots", False)
        max_chars = settings.get("max_characters", DEFAULT_MAX_CHARS)  # some instances allow more than 500
        self.constraints = Constraints(max_length=max_chars - MENTION_ROOM, markdown=False, ai_drafts_allowed=True,
                                       notes=NOTES, length=status_length)

    # ------------------------------------------------------------ conversion
    def _to_thread(self, status: dict, source: str) -> Thread:
        text = html_to_text(status.get("content", ""))
        if status.get("spoiler_text"):
            text = f"CW: {status['spoiler_text']}\n\n{text}"
        first_line = text.split("\n", 1)[0]
        return Thread(
            platform=self.name, id=status["id"], source=source,
            title=first_line[:120] + ("…" if len(first_line) > 120 else ""), body=text,
            url=status.get("url") or status["uri"], author=status["account"]["acct"],
            score=status.get("favourites_count", 0), num_comments=status.get("replies_count", 0),
            created_at=_time(status["created_at"]),
        )

    def _wanted(self, status: dict, since: datetime) -> bool:
        lang = status.get("language")
        return (
            status.get("reblog") is None                                  # boosts: the original shows up itself
            and status.get("in_reply_to_id") is None                      # start of a conversation
            and status["account"]["id"] != self.client.me["id"]           # not your own posts
            and (self.include_bots or not status["account"].get("bot"))   # skip automated feed accounts
            and status.get("visibility") in ("public", "unlisted")
            and html_to_text(status.get("content", "")) != ""
            and (not lang or not self.languages or lang in self.languages)
            and _time(status["created_at"]) >= since
        )

    # ------------------------------------------------------------ discovery
    def _timeline(self, path: str, since: datetime, limit: int, **params) -> list[dict]:
        """Newest first, paging back until `limit` statuses or ones older than `since`."""
        out: list[dict] = []
        max_id = None
        while len(out) < limit:
            page = self.client.get(path, limit=min(PAGE, limit - len(out)), max_id=max_id, **params)
            out += page
            if len(page) < PAGE or not page or _time(page[-1]["created_at"]) < since:
                break
            max_id = page[-1]["id"]
        return out

    def _statuses(self, source: str, since: datetime, limit: int) -> list[dict]:
        if source.startswith("#"):
            return self._timeline(f"/api/v1/timelines/tag/{source[1:]}", since, limit)
        if source == "@home":
            return self._timeline("/api/v1/timelines/home", since, limit)
        if source == "@local":
            return self._timeline("/api/v1/timelines/public", since, limit, local="true")
        # Full-text search only covers posts from people who opted in to being searchable.
        return self.client.get("/api/v2/search", q=source, type="statuses", limit=min(PAGE, limit))["statuses"]

    def discover(self, sources: list[str], since: datetime, limit: int) -> list[Thread]:
        """Sources: hashtags ("#java"), "@home" (people you follow), "@local" (your instance), or search words."""
        seen: dict[str, Thread] = {}
        for source in sources:
            for status in self._statuses(source, since, limit):
                if status["id"] not in seen and self._wanted(status, since):
                    seen[status["id"]] = self._to_thread(status, source)
        return list(seen.values())

    def fetch_context(self, thread: Thread) -> Thread:
        status = self.client.get(f"/api/v1/statuses/{thread.id}")
        thread.score, thread.num_comments = status.get("favourites_count", 0), status.get("replies_count", 0)
        replies = [s for s in self.client.get(f"/api/v1/statuses/{thread.id}/context")["descendants"]
                   if s.get("in_reply_to_id") == thread.id]
        thread.num_comments = max(thread.num_comments, len(replies))
        replies.sort(key=lambda s: s.get("favourites_count", 0), reverse=True)
        thread.top_comments = [Comment(author=s["account"]["acct"], body=html_to_text(s.get("content", "")),
                                       score=s.get("favourites_count", 0)) for s in replies[:TOP_REPLIES]]
        return thread

    def thread_from_url(self, url: str) -> Thread | None:
        """A status link from any instance -> a Thread, fetched through yours (so you can reply to it)."""
        if not WEB_STATUS_URL.match(url):
            return None
        found = self.client.get("/api/v2/search", q=url, type="statuses", resolve="true", limit=1)["statuses"]
        if not found:
            return None
        return self.fetch_context(self._to_thread(found[0], "mastodon"))

    # ------------------------------------------------------------ posting
    def _post(self, text: str, **extra) -> str:
        body = {"status": text, "visibility": self.visibility, **extra}
        if len(self.languages) == 1:
            body["language"] = next(iter(self.languages))
        created = self.client.post("/api/v1/statuses", body)
        return created.get("url") or created["uri"]

    def submit(self, reply: Reply) -> str:
        parent = self.client.get(f"/api/v1/statuses/{reply.thread_id}")
        mention = f"@{parent['account']['acct']}"
        text = reply.text if reply.text.lstrip().lower().startswith(mention.lower()) else f"{mention} {reply.text}"
        return self._post(text, in_reply_to_id=reply.thread_id)

    def publish(self, text: str, link: LinkCard | None = None) -> str:
        if link and link.uri not in text:  # Mastodon builds the preview card from the first link in the text
            text = f"{text}\n\n{link.uri}"
        return self._post(text)

    def my_activity(self, limit: int) -> list[ActivityItem]:
        me = self.client.me
        items: list[ActivityItem] = []
        max_id = None
        while len(items) < limit:
            page = self.client.get(f"/api/v1/accounts/{me['id']}/statuses", exclude_reblogs="true",
                                   limit=min(PAGE, limit - len(items)), max_id=max_id)
            if not page:
                break
            items += [ActivityItem(
                platform=self.name, id=s["id"], source="replies" if s.get("in_reply_to_id") else "posts",
                body=html_to_text(s.get("content", "")), score=s.get("favourites_count", 0),
                created_at=_time(s["created_at"]), permalink=s.get("url") or s["uri"]) for s in page]
            max_id = page[-1]["id"]
        return items[:limit]
