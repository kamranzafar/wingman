# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Reddit without API access: reads public RSS feeds, and you post from your own browser.

Discovery: /r/<sub>/new/.rss and /r/<sub>/rising/.rss
Context:   /r/<sub>/comments/<id>/.rss (top comments)
Activity:  /user/<name>/comments/.rss (your public comments; RSS carries no scores)
Posting:   the draft is copied to your clipboard and the thread opened in your browser.
"""

from __future__ import annotations

import logging
import os
import time
import urllib.error
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from xml.etree import ElementTree

from bs4 import BeautifulSoup

from wingman import __version__
from wingman.core import http
from wingman.core.models import ActivityItem, Comment, Reply, Thread
from wingman.errors import ConfigError, WingmanError
from wingman.platforms.reddit import BASE_URL as BASE
from wingman.platforms.reddit import make_constraints

log = logging.getLogger(__name__)
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_ATTEMPTS = 4


def _redact(url: str) -> str:
    """Strip the query string so private feed tokens never reach logs or the terminal."""
    return url.split("?", 1)[0]


class RedditRSSPlatform:
    name = "reddit"
    posting = "manual"       # review copies the draft and opens the browser; you submit
    has_scores = False       # RSS has no vote counts
    constraints = make_constraints(rules_known=False)

    def __init__(self, settings: dict):
        self.settings = settings
        self.delay = float(settings.get("request_delay_seconds", 2.0))
        self._last = 0.0
        self.username = os.environ.get("REDDIT_USERNAME", "").removeprefix("u/").removeprefix("/u/")

    # ------------------------------------------------------------ fetching
    def _with_token(self, url: str) -> str:
        """Attach your private feed token (feed=…&user=…) so Reddit serves the feed as you."""
        feed = os.environ.get("REDDIT_FRONTPAGE_FEED", "").strip()
        if not feed or "feed=" in url:
            return url
        token = parse_qs(urlsplit(feed).query)
        extra = {k: token[k][0] for k in ("feed", "user") if k in token}
        if not extra:
            return url
        return url + ("&" if "?" in url else "?") + urlencode(extra)

    def _fetch(self, url: str) -> ElementTree.Element:
        url = self._with_token(url)
        ua = f"wingman/{__version__} (personal RSS reader" + (f"; /u/{self.username})" if self.username else ")")
        for attempt in range(MAX_ATTEMPTS):
            wait = self.delay - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                body, _ = http.get_bytes(url, headers={"User-Agent": ua})
                return ElementTree.fromstring(body)
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < MAX_ATTEMPTS - 1:
                    time.sleep(int(e.headers.get("Retry-After", 0) or 0) or 10 * (attempt + 1))
                    continue
                if e.code in (403, 404):
                    raise LookupError(f"{_redact(url)} returned {e.code} (blocked, private or misspelled?)") from e
                raise WingmanError(f"{_redact(url)} returned HTTP {e.code}") from None
        raise WingmanError(f"Reddit kept rate-limiting {_redact(url)}; try again later.")

    def _frontpage_urls(self, listings: list[str], limit: int) -> list[str]:
        """Your private front-page feed (from reddit.com/prefs/feeds), one URL per listing."""
        feed = os.environ.get("REDDIT_FRONTPAGE_FEED", "").strip()
        if not feed:
            raise ConfigError("Add your private front-page RSS link from https://www.reddit.com/prefs/feeds "
                               "to .env as REDDIT_FRONTPAGE_FEED to use @frontpage.")
        parts = urlsplit(feed)
        query = parse_qs(parts.query)
        query["limit"] = [str(min(limit, 100))]
        urls = []
        for listing in listings:
            path = "/.rss" if listing == "hot" else f"/{listing}/.rss"
            urls.append(urlunsplit((parts.scheme, parts.netloc, path, urlencode(query, doseq=True), "")))
        return urls

    @staticmethod
    def _text(entry: ElementTree.Element, tag: str) -> str:
        el = entry.find(ATOM + tag)
        return (el.text or "").strip() if el is not None else ""

    @staticmethod
    def _body(entry: ElementTree.Element) -> str:
        html = RedditRSSPlatform._text(entry, "content")
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        md = soup.find("div", class_="md")  # self-text / comment body; the rest is "submitted by" boilerplate
        return md.get_text("\n", strip=True) if md else ""

    @staticmethod
    def _author(entry: ElementTree.Element) -> str:
        el = entry.find(f"{ATOM}author/{ATOM}name")
        return (el.text or "").removeprefix("/u/") if el is not None else "[deleted]"

    @staticmethod
    def _when(entry: ElementTree.Element) -> datetime:
        raw = RedditRSSPlatform._text(entry, "published") or RedditRSSPlatform._text(entry, "updated")
        return datetime.fromisoformat(raw)

    @staticmethod
    def _link(entry: ElementTree.Element) -> str:
        el = entry.find(ATOM + "link")
        return el.get("href", "") if el is not None else ""

    @staticmethod
    def _source(entry: ElementTree.Element, fallback: str) -> str:
        el = entry.find(ATOM + "category")
        return el.get("term", fallback) if el is not None else fallback

    # ------------------------------------------------------------ Platform
    def discover(self, sources: list[str], since: datetime, limit: int) -> list[Thread]:
        seen: dict[str, Thread] = {}
        listings = self.settings.get("listings", ["new", "rising"])
        excluded = {s[1:].lower() for s in sources if s.startswith("-")}

        feeds: list[tuple[str, str]] = []  # (label, url)
        if "@frontpage" in sources:
            feeds += [("front page", u) for u in self._frontpage_urls(listings, limit)]
        for sub in [s for s in sources if not s.startswith(("@", "-"))]:
            feeds += [(f"r/{sub}", f"{BASE}/r/{sub}/{listing}/.rss?limit={min(limit, 100)}") for listing in listings]

        failed: set[str] = set()
        for label, url in feeds:
            if label in failed:
                continue
            try:
                root = self._fetch(url)
            except LookupError as e:
                log.warning("skip %s: %s", label, e)
                failed.add(label)
                continue
            for entry in root.iter(ATOM + "entry"):
                tid = self._text(entry, "id").removeprefix("t3_")
                created = self._when(entry)
                source = self._source(entry, label.removeprefix("r/"))
                if not tid or created < since or tid in seen or source.lower() in excluded:
                    continue
                seen[tid] = Thread(
                    platform=self.name, id=tid, source=source,
                    title=self._text(entry, "title"), body=self._body(entry)[:6000],
                    url=self._link(entry), author=self._author(entry),
                    score=0, num_comments=0, created_at=created,
                )
        return list(seen.values())

    def fetch_context(self, thread: Thread) -> Thread:
        root = self._fetch(f"{BASE}/r/{thread.source}/comments/{thread.id}/.rss?sort=top&limit=15")
        comments = [e for e in root.iter(ATOM + "entry") if not self._text(e, "id").startswith("t3_")]
        thread.top_comments = [Comment(author=self._author(e), body=self._body(e)[:1500], score=0)
                               for e in comments[:8]]
        thread.num_comments = len(comments)
        return thread

    def thread_from_url(self, url: str) -> Thread | None:
        return None  # no Reddit access in this mode: the thread is pasted

    def publish(self, text: str, link=None) -> str:
        raise WingmanError("New posts aren't supported on Reddit; wingman only drafts replies there.")

    def submit(self, reply: Reply) -> str:
        raise WingmanError("Manual mode: you post from your browser.")

    def my_activity(self, limit: int) -> list[ActivityItem]:
        if not self.username:
            raise ConfigError("Set REDDIT_USERNAME in .env to read your past comments.")
        root = self._fetch(f"{BASE}/user/{self.username}/comments/.rss?limit={min(limit, 100)}")
        return [
            ActivityItem(platform=self.name, id=self._text(e, "id").removeprefix("t1_"),
                         source=self._source(e, ""), body=self._body(e), score=0,
                         created_at=self._when(e), permalink=self._link(e))
            for e in root.iter(ATOM + "entry")
        ]
