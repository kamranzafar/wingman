"""Reddit adapter built on PRAW. Credentials come from environment variables (.env)."""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import praw

from wingman import __version__
from wingman.core.models import ActivityItem, Comment, Reply, Thread
from wingman.errors import ConfigError, WingmanError
from wingman.platforms.reddit import BASE_URL, make_constraints

log = logging.getLogger(__name__)
REQUIRED_ENV = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USERNAME", "REDDIT_PASSWORD"]


def _ts(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=UTC)


class RedditPlatform:
    name = "reddit"
    posting = "api"
    has_scores = True
    constraints = make_constraints(rules_known=True)

    def __init__(self, settings: dict):
        missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
        if missing:
            raise ConfigError(f"Missing Reddit credentials in .env: {', '.join(missing)}")
        username = os.environ["REDDIT_USERNAME"]
        self.settings = settings
        self.reddit = praw.Reddit(
            client_id=os.environ["REDDIT_CLIENT_ID"],
            client_secret=os.environ["REDDIT_CLIENT_SECRET"],
            username=username,
            password=os.environ["REDDIT_PASSWORD"],
            user_agent=f"script:wingman:v{__version__} (by /u/{username})",
        )
        self._rules_cache: dict[str, list[str]] = {}

    def _to_thread(self, s) -> Thread:
        return Thread(
            platform=self.name,
            id=s.id,
            source=s.subreddit.display_name,
            title=s.title,
            body=(s.selftext or "")[:6000],
            url=f"{BASE_URL}{s.permalink}",
            author=str(s.author) if s.author else "[deleted]",
            score=s.score,
            num_comments=s.num_comments,
            created_at=_ts(s.created_utc),
        )

    def resolve_sources(self, sources: list[str]) -> list[str]:
        """Expand "@subscribed" into the subreddits you've joined; drop excluded ones ("-name")."""
        excluded = {s[1:].lower() for s in sources if s.startswith("-")}
        out: dict[str, str] = {}
        for s in sources:
            if s == "@subscribed":
                for sub in self.reddit.user.subreddits(limit=None):
                    if not sub.display_name.startswith("u_"):  # skip user profile "subreddits"
                        out.setdefault(sub.display_name.lower(), sub.display_name)
            elif not s.startswith("-"):
                out.setdefault(s.lower(), s)
        return [name for key, name in out.items() if key not in excluded]

    def discover(self, sources: list[str], since: datetime, limit: int) -> list[Thread]:
        seen: dict[str, Thread] = {}
        for sub in self.resolve_sources(sources):
            subreddit = self.reddit.subreddit(sub)
            for listing in (subreddit.new(limit=limit), subreddit.rising(limit=limit)):
                for s in listing:
                    if s.stickied or s.locked or s.archived or s.over_18:
                        continue
                    if _ts(s.created_utc) < since:
                        continue
                    seen.setdefault(s.id, self._to_thread(s))
        return list(seen.values())

    def _rules(self, sub: str) -> list[str]:
        if sub not in self._rules_cache:
            try:
                self._rules_cache[sub] = [
                    f"{r.short_name}: {r.description}".strip(": ")
                    for r in self.reddit.subreddit(sub).rules
                ]
            except Exception:  # noqa: BLE001 - private sub, no rules; draft without them
                self._rules_cache[sub] = []
        return self._rules_cache[sub]

    def fetch_context(self, thread: Thread) -> Thread:
        s = self.reddit.submission(id=thread.id)
        s.comment_sort = "top"
        s.comments.replace_more(limit=0)
        thread.top_comments = [
            Comment(author=str(c.author) if c.author else "[deleted]", body=c.body[:1500], score=c.score)
            for c in list(s.comments)[:8]
        ]
        thread.score, thread.num_comments = s.score, s.num_comments
        thread.source_rules = self._rules(thread.source)
        return thread

    def thread_from_url(self, url: str) -> Thread | None:
        try:
            return self.fetch_context(self._to_thread(self.reddit.submission(url=url)))
        except Exception as e:  # noqa: BLE001 - not a submission URL, deleted, private: fall back to pasting
            log.debug("couldn't load %s: %s", url, e)
            return None

    def publish(self, text: str, link=None) -> str:
        raise WingmanError("New posts aren't supported on Reddit; wingman only drafts replies there.")

    def submit(self, reply: Reply) -> str:
        comment = self.reddit.submission(id=reply.thread_id).reply(reply.text)
        return f"{BASE_URL}{comment.permalink}"

    def my_activity(self, limit: int) -> list[ActivityItem]:
        me = self.reddit.user.me()
        return [
            ActivityItem(
                platform=self.name,
                id=c.id,
                source=c.subreddit.display_name,
                body=c.body,
                score=c.score,
                created_at=_ts(c.created_utc),
                permalink=f"{BASE_URL}{c.permalink}",
            )
            for c in me.comments.new(limit=limit)
        ]
