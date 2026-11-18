"""Reddit with no Reddit access at all: you bring the thread (`wingman draft <url>`), you post it.

Nothing in this adapter touches Reddit. Threads come from text you copy out of your browser;
replies are copied to your clipboard and posted by you.
"""

from __future__ import annotations

from datetime import datetime

from wingman.core.models import ActivityItem, Reply, Thread
from wingman.errors import WingmanError
from wingman.platforms.reddit import make_constraints


class RedditPastePlatform:
    name = "reddit"
    posting = "manual"
    has_scores = False
    constraints = make_constraints(rules_known=False)

    def __init__(self, settings: dict):
        self.settings = settings

    def discover(self, sources: list[str], since: datetime, limit: int) -> list[Thread]:
        raise WingmanError("Paste mode has no automatic discovery. Find a thread in your browser, "
                           "copy the page, and run `wingman draft <thread url>`.")

    def fetch_context(self, thread: Thread) -> Thread:
        return thread  # everything came from the pasted text

    def thread_from_url(self, url: str) -> Thread | None:
        return None  # no Reddit access in this mode: the thread is pasted

    def publish(self, text: str, link=None) -> str:
        raise WingmanError("New posts aren't supported on Reddit; wingman only drafts replies there.")

    def submit(self, reply: Reply) -> str:
        raise WingmanError("Paste mode: you post from your browser.")

    def my_activity(self, limit: int) -> list[ActivityItem]:
        return []  # stats falls back to what you've logged through wingman
