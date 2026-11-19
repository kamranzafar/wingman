# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Bluesky adapter: search/feed discovery, thread context, replies and your post history over XRPC."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from wingman.core.models import ActivityItem, Comment, Constraints, LinkCard, Reply, Thread, now_utc
from wingman.platforms.bluesky.client import DEFAULT_SERVICE, XrpcClient
from wingman.platforms.bluesky.richtext import grapheme_length, link_facets

log = logging.getLogger(__name__)

POST_COLLECTION = "app.bsky.feed.post"
MAX_POST_GRAPHEMES = 300
TOP_REPLIES = 8
WEB_POST_URL = re.compile(r"https?://bsky\.app/profile/(?P<actor>[^/]+)/post/(?P<rkey>[A-Za-z0-9]+)")

CONSTRAINTS = Constraints(
    max_length=MAX_POST_GRAPHEMES,
    markdown=False,
    ai_drafts_allowed=True,
    notes=("Bluesky: short, conversational replies; the 300-character limit includes any link, so only "
           "include one if it's essential. No markdown (it shows as literal symbols), no hashtag stuffing, "
           "no thread-style numbering. One clear point beats a summary."),
    length=grapheme_length,
)


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _rkey(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def web_url(handle: str, uri: str) -> str:
    return f"https://bsky.app/profile/{handle}/post/{_rkey(uri)}"


class BlueskyPlatform:
    name = "bluesky"
    posting = "api"
    has_scores = True  # likes
    constraints = CONSTRAINTS

    def __init__(self, settings: dict, client: XrpcClient | None = None):
        self.settings = settings
        self.client = client or XrpcClient(os.environ.get("BLUESKY_HANDLE", ""),
                                           os.environ.get("BLUESKY_APP_PASSWORD", ""),
                                           settings.get("service", DEFAULT_SERVICE))
        self.languages = set(settings.get("languages", ["en"]))

    # ------------------------------------------------------------ conversion
    def _to_thread(self, post: dict, source: str) -> Thread:
        record = post.get("record", {})
        text = record.get("text", "")
        first_line = text.strip().split("\n", 1)[0]
        return Thread(
            platform=self.name, id=post["uri"], source=source,
            title=first_line[:120] + ("…" if len(first_line) > 120 else ""), body=text,
            url=web_url(post["author"]["handle"], post["uri"]),
            author=post["author"]["handle"], score=post.get("likeCount", 0),
            num_comments=post.get("replyCount", 0),
            created_at=_time(record.get("createdAt") or post["indexedAt"]),
        )

    def _wanted(self, post: dict, since: datetime) -> bool:
        record = post.get("record", {})
        langs = set(record.get("langs") or [])
        return (
            record.get("text", "").strip() != ""                    # image-only posts: nothing to reply to
            and "reply" not in record                               # start of a conversation, not mid-thread
            and post["author"]["did"] != self.client.did            # not your own posts
            and (not langs or not self.languages or bool(langs & self.languages))
            and _time(record.get("createdAt") or post["indexedAt"]) >= since
        )

    # ------------------------------------------------------------ Platform
    def discover(self, sources: list[str], since: datetime, limit: int) -> list[Thread]:
        """Sources are search queries ("python asyncio", "#rustlang") or custom feed URIs (at://...)."""
        seen: dict[str, Thread] = {}
        for source in sources:
            if source.startswith("at://"):
                posts = [item["post"] for item in self.client.get("app.bsky.feed.getFeed", feed=source,
                                                                  limit=min(limit, 100))["feed"]]
            else:
                posts = self.client.get("app.bsky.feed.searchPosts", q=source, sort="latest",
                                        since=since.isoformat().replace("+00:00", "Z"),
                                        limit=min(limit, 100))["posts"]
            for post in posts:
                if post["uri"] not in seen and self._wanted(post, since):
                    seen[post["uri"]] = self._to_thread(post, source)
        return list(seen.values())

    def fetch_context(self, thread: Thread) -> Thread:
        data = self.client.get("app.bsky.feed.getPostThread", uri=thread.id, depth=1, parentHeight=0)["thread"]
        post = data.get("post", {})
        thread.score, thread.num_comments = post.get("likeCount", thread.score), post.get("replyCount", 0)
        replies = [r["post"] for r in data.get("replies", []) if "post" in r]
        replies.sort(key=lambda p: p.get("likeCount", 0), reverse=True)
        thread.top_comments = [Comment(author=p["author"]["handle"], body=p["record"].get("text", ""),
                                       score=p.get("likeCount", 0)) for p in replies[:TOP_REPLIES]]
        return thread

    def thread_from_url(self, url: str) -> Thread | None:
        """A bsky.app post link -> a Thread with its replies (no copy-paste needed)."""
        m = WEB_POST_URL.match(url)
        if not m:
            return None
        actor = m.group("actor")
        did = actor if actor.startswith("did:") else \
            self.client.get("com.atproto.identity.resolveHandle", handle=actor)["did"]
        uri = f"at://{did}/{POST_COLLECTION}/{m.group('rkey')}"
        [post] = self.client.get("app.bsky.feed.getPosts", uris=[uri])["posts"]
        return self.fetch_context(self._to_thread(post, "bluesky"))

    def _record(self, text: str) -> dict:
        record = {"$type": POST_COLLECTION, "text": text, "createdAt": now_utc().isoformat().replace("+00:00", "Z"),
                  "langs": sorted(self.languages) or ["en"]}
        facets = link_facets(text)
        if facets:
            record["facets"] = facets
        return record

    def _create(self, record: dict) -> str:
        created = self.client.post("com.atproto.repo.createRecord",
                                   {"repo": self.client.did, "collection": POST_COLLECTION, "record": record})
        return web_url(self.client.handle, created["uri"])

    def submit(self, reply: Reply) -> str:
        [parent] = self.client.get("app.bsky.feed.getPosts", uris=[reply.thread_id])["posts"]
        parent_ref = {"uri": parent["uri"], "cid": parent["cid"]}
        root_ref = parent["record"].get("reply", {}).get("root", parent_ref)  # reply to a reply keeps its root
        record = self._record(reply.text)
        record["reply"] = {"root": root_ref, "parent": parent_ref}
        return self._create(record)

    def publish(self, text: str, link: LinkCard | None = None) -> str:
        record = self._record(text)
        if link:  # preview card under the post; the link in the text stays clickable too
            record["embed"] = {"$type": "app.bsky.embed.external",
                               "external": {"uri": link.uri, "title": link.title, "description": link.description}}
        return self._create(record)

    def my_activity(self, limit: int) -> list[ActivityItem]:
        items: list[ActivityItem] = []
        cursor = None
        while len(items) < limit:
            page = self.client.get("app.bsky.feed.getAuthorFeed", actor=self.client.did,
                                   filter="posts_with_replies", limit=min(100, limit - len(items)), cursor=cursor)
            for entry in page["feed"]:
                post = entry["post"]
                if post["author"]["did"] != self.client.did or "reason" in entry:  # skip reposts
                    continue
                record = post["record"]
                items.append(ActivityItem(
                    platform=self.name, id=post["uri"], source="replies" if "reply" in record else "posts",
                    body=record.get("text", ""), score=post.get("likeCount", 0),
                    created_at=_time(record["createdAt"]), permalink=web_url(self.client.handle, post["uri"])))
            cursor = page.get("cursor")
            if not cursor or not page["feed"]:
                break
        return items[:limit]
