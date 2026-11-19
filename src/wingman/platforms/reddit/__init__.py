"""Reddit adapters, picked by `mode` in [platforms.reddit]:

paste.py - (default) no Reddit access: `wingman draft <url>` with text copied from your browser
api.py   - PRAW; scans, drafts and posts. Needs Reddit API approval (Responsible Builder Policy)
rss.py   - feed reader + manual posting. Reddit currently blocks non-browser feed clients,
           so this mode can't read threads today
"""

from __future__ import annotations

from wingman.core.models import Constraints
from wingman.errors import ConfigError

ETIQUETTE = ("Reddit values genuine, specific, conversational replies. No marketing tone, "
             "no hashtags, no emoji spam, no sign-offs.")
MAX_COMMENT_LENGTH = 10_000
BASE_URL = "https://www.reddit.com"


def make_constraints(rules_known: bool) -> Constraints:
    rules = ("Subreddit rules override everything." if rules_known else
             "If the community's rules weren't provided, keep to common norms: "
             "no self-promotion unless clearly useful.")
    return Constraints(max_length=MAX_COMMENT_LENGTH, markdown=True, ai_drafts_allowed=True,
                       notes=f"{ETIQUETTE} {rules}")


SETTINGS = frozenset({"mode", "listings", "request_delay_seconds"})  # keys beyond platforms.COMMON


def create(settings: dict):
    mode = settings.get("mode", "paste")
    if mode == "api":
        from wingman.platforms.reddit.api import RedditPlatform

        return RedditPlatform(settings)
    if mode == "manual":
        from wingman.platforms.reddit.rss import RedditRSSPlatform

        return RedditRSSPlatform(settings)
    if mode == "paste":
        from wingman.platforms.reddit.paste import RedditPastePlatform

        return RedditPastePlatform(settings)
    raise ConfigError(f"Unknown reddit mode '{mode}'. Use paste, api or manual.")
