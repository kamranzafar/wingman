"""Platform adapter registry.

Each platform lives in its own package (platforms/<name>/) exposing `create(settings) -> Platform`
and `SETTINGS`, the [platforms.<name>] keys it reads beyond the COMMON ones. Add a new platform by
creating that package and listing it in PLATFORMS.
"""

from __future__ import annotations

from difflib import get_close_matches
from importlib import import_module

from wingman.errors import ConfigError
from wingman.platforms.base import Platform

PLATFORMS = ("reddit", "bluesky", "mastodon", "github")
# Keys every platform section accepts (read by the scan/review commands and the config).
COMMON = frozenset({"enabled", "sources", "keywords", "max_age_hours", "per_source_limit"})
# Which platform a pasted thread URL belongs to, so `wingman draft <url>` needs no --platform.
URL_HOSTS = {"reddit.com": "reddit", "www.reddit.com": "reddit", "old.reddit.com": "reddit",
             "redd.it": "reddit", "bsky.app": "bluesky"}


def platform_for_url(url: str) -> str | None:
    from urllib.parse import urlsplit

    from wingman.platforms.github.api import WEB_DISCUSSION_URL
    from wingman.platforms.mastodon.api import WEB_STATUS_URL

    host = URL_HOSTS.get(urlsplit(url).netloc.lower())
    if host:
        return host
    if WEB_DISCUSSION_URL.match(url):
        return "github"
    # Mastodon runs on thousands of hosts; recognise its status-link shape instead.
    return "mastodon" if WEB_STATUS_URL.match(url) else None


def check_settings(name: str, settings: dict, extra: frozenset[str]) -> None:
    """Unknown keys in [platforms.<name>] are errors (a typo would otherwise be silently ignored)."""
    allowed = COMMON | extra
    unknown = sorted(set(settings) - allowed)
    if unknown:
        hints = []
        for key in unknown:
            close = get_close_matches(key, allowed, n=1)
            hints.append(f"'{key}'" + (f" (did you mean '{close[0]}'?)" if close else ""))
        raise ConfigError(f"Unknown setting{'s' if len(unknown) > 1 else ''} in [platforms.{name}]: "
                          f"{', '.join(hints)}. Allowed: {', '.join(sorted(allowed))}")


def get_platform(name: str, settings: dict) -> Platform:
    if name not in PLATFORMS:
        raise ConfigError(f"Unknown platform '{name}'. Available: {', '.join(PLATFORMS)}")
    package = import_module(f"wingman.platforms.{name}")
    check_settings(name, settings, package.SETTINGS)
    return package.create(settings)
