# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Hacker News adapter over the Algolia HN Search API (https://hn.algolia.com/api).

Discovery: /search_by_date (stories, Ask HN, Show HN, front page)
Context:   /items/<id> (the story and its comment tree)
Activity:  /search_by_date?tags=comment,author_<you> (HN exposes no comment scores)
Posting:   manual. HN has no write API: replies and new posts are drafted, you edit and submit them.
"""

from __future__ import annotations

import os
import re
import time
import urllib.error
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from wingman.core import http
from wingman.core.models import ActivityItem, Comment, Constraints, LinkCard, Reply, Thread
from wingman.errors import WingmanError
from wingman.platforms.mastodon.text import html_to_text

API = "https://hn.algolia.com/api/v1"
ITEM_URL = "https://news.ycombinator.com/item?id={}"
SUBMIT_URL = "https://news.ycombinator.com/submitlink"
MAX_TITLE_CHARS = 80
WEB_ITEM_URL = re.compile(r"^https?://news\.ycombinator\.com/item\?(?:.*&)?id=(?P<id>\d+)")
MAX_COMMENT_CHARS = 10_000
TOP_COMMENTS = 8
# "@ask kafka" -> Ask HN posts matching "kafka"; "@ask" alone -> all recent Ask HN posts.
TAGS = {"@ask": "ask_hn", "@show": "show_hn", "@front": "front_page"}
SOURCE_NAMES = {"ask_hn": "Ask HN", "show_hn": "Show HN", "front_page": "front page"}

CONSTRAINTS = Constraints(
    max_length=MAX_COMMENT_CHARS,
    markdown=False,
    notes=("Hacker News: plain text only (blank lines between paragraphs, *italics*, indent code by two "
           "spaces; links are pasted bare). Substantive, curious and specific, written like an engineer "
           "talking to peers. No hashtags, emoji, hype, marketing tone, greetings or sign-offs. Don't "
           "summarise the thread back; add something: experience, a trade-off, a correction, a source. "
           "A new post is the text of an Ask HN / Show HN or the note under a shared link: state plainly "
           "what it is and what you want to discuss."),
)
GUIDELINES = ["HN guidelines: be kind and substantive; no snark or flamebait; no self-promotion beyond what's "
              "directly relevant (https://news.ycombinator.com/newsguidelines.html)"]


def _text(html: str | None) -> str:
    """HN HTML (paragraphs separated by bare <p>, full-href links) -> plain text."""
    return html_to_text(re.sub(r"<p>", "\n\n", html or "")) if html else ""


def _time(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def parse_source(source: str) -> tuple[str, str]:
    """A configured source -> (tags, query). Plain words search stories."""
    head, _, rest = source.strip().partition(" ")
    if head.lower() in TAGS:
        return TAGS[head.lower()], rest.strip()
    return "story", source.strip()


class HackerNewsPlatform:
    name = "hackernews"
    posting = "manual"   # no write API: review copies your reply and opens the thread
    has_scores = False   # HN hides comment scores, so `stats` can't show yours
    constraints = CONSTRAINTS

    def __init__(self, settings: dict, get_json=None):
        self.settings = settings
        self.min_points = int(settings.get("min_points", 0))
        self.username = os.environ.get("HN_USERNAME", "").strip()
        self._get_json = get_json or http.get_json

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{API}/{path}" + (f"?{urlencode(params)}" if params else "")
        for attempt in range(3):
            try:
                return self._get_json(url)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise LookupError(path) from e
                if e.code == 429 and attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                raise WingmanError(f"Hacker News search failed (HTTP {e.code}).") from None
            except urllib.error.URLError as e:
                raise WingmanError(f"Couldn't reach hn.algolia.com: {e.reason}") from None
        raise WingmanError("Hacker News search kept rate-limiting; try again later.")

    # ------------------------------------------------------------ conversion
    def _to_thread(self, h: dict, source: str) -> Thread:
        sid = str(h.get("objectID") or h["id"])
        body = _text(h.get("story_text") or h.get("text"))
        if h.get("url"):
            body = f"Link: {h['url']}" + (f"\n\n{body}" if body else "")
        return Thread(
            platform=self.name, id=sid, source=source, title=h.get("title") or "", body=body[:6000],
            url=ITEM_URL.format(sid), author=h.get("author") or "[deleted]", score=h.get("points") or 0,
            num_comments=h.get("num_comments") or 0, created_at=_time(h["created_at_i"]), source_rules=GUIDELINES,
        )

    def _wanted(self, h: dict, since: datetime) -> bool:
        return (
            bool(h.get("title"))
            and _time(h["created_at_i"]) >= since
            and (h.get("points") or 0) >= self.min_points
            and not (self.username and (h.get("author") or "").lower() == self.username.lower())
        )

    # ------------------------------------------------------------ Platform
    def discover(self, sources: list[str], since: datetime, limit: int) -> list[Thread]:
        """Sources: search words ("postgres"), "@ask", "@show", "@front", or those with words ("@ask kafka")."""
        seen: dict[str, Thread] = {}
        for source in sources or ["@front"]:
            tags, query = parse_source(source)
            hits = self._get("search_by_date", query=query, tags=tags, hitsPerPage=min(limit, 100),
                             numericFilters=f"created_at_i>={int(since.timestamp())}")["hits"]
            label = " ".join(filter(None, [SOURCE_NAMES.get(tags), query])) or source
            for h in hits:
                if h.get("objectID") not in seen and self._wanted(h, since):
                    seen[h["objectID"]] = self._to_thread(h, label)
        return list(seen.values())

    def _comments(self, item: dict) -> list[Comment]:
        """Top-level comments, the most-discussed first (HN shows no comment scores)."""
        def size(c: dict) -> int:
            return 1 + sum(size(k) for k in c.get("children") or [])

        top = [c for c in item.get("children") or [] if c.get("text")]
        top.sort(key=size, reverse=True)
        return [Comment(author=c.get("author") or "[deleted]", body=_text(c["text"])[:1500], score=0)
                for c in top[:TOP_COMMENTS]]

    def fetch_context(self, thread: Thread) -> Thread:
        item = self._get(f"items/{thread.id}")
        thread.top_comments = self._comments(item)
        thread.score = item.get("points") or thread.score
        thread.num_comments = sum(1 for _ in _walk(item)) - 1
        return thread

    def thread_from_url(self, url: str) -> Thread | None:
        """A story link, or a comment link (you reply to that comment; the story is context)."""
        m = WEB_ITEM_URL.match(url)
        if not m:
            return None
        try:
            item = self._get(f"items/{m.group('id')}")
        except LookupError:
            raise WingmanError(f"No Hacker News item {m.group('id')}.") from None
        if item.get("type") == "story":
            return self.fetch_context(self._to_thread(item, "Hacker News"))
        story = self._get(f"items/{item['story_id']}")
        thread = self._to_thread(story, "Hacker News")
        thread.body += f"\n\nYou're replying to this comment by {item.get('author')}:\n{_text(item.get('text'))}"
        thread.url = ITEM_URL.format(item["id"])  # opens the comment, where HN's reply box is
        thread.top_comments = self._comments(item) or self._comments(story)
        thread.num_comments = sum(1 for _ in _walk(story)) - 1
        return thread

    def submit(self, reply: Reply) -> str:
        raise WingmanError("Hacker News has no posting API: you post from your browser.")

    def publish(self, text: str, link: LinkCard | None = None) -> str:
        raise WingmanError("Hacker News has no posting API: you submit new posts from your browser.")

    def new_post_url(self, title: str, link: LinkCard | None = None) -> str:
        """HN's submit form with the title (and the shared link) filled in; the text is pasted."""
        title = (link.title if link else title).strip()[:MAX_TITLE_CHARS]
        params = {"t": title, **({"u": link.uri} if link else {})}
        return f"{SUBMIT_URL}?{urlencode(params, quote_via=quote)}"

    def my_activity(self, limit: int) -> list[ActivityItem]:
        if not self.username:
            raise WingmanError("Set HN_USERNAME in .env to read your past comments.")
        items: list[ActivityItem] = []
        page = 0
        while len(items) < limit:
            out = self._get("search_by_date", tags=f"comment,author_{self.username}",
                            hitsPerPage=min(100, limit - len(items)), page=page)
            items += [ActivityItem(platform=self.name, id=h["objectID"], source=h.get("story_title") or "",
                                   body=_text(h.get("comment_text")), score=0, created_at=_time(h["created_at_i"]),
                                   permalink=ITEM_URL.format(h["objectID"])) for h in out["hits"]]
            page += 1
            if page >= out.get("nbPages", 0) or not out["hits"]:
                break
        return items[:limit]


def _walk(item: dict):
    yield item
    for child in item.get("children") or []:
        yield from _walk(child)
