"""Platform adapter registry.

Each platform lives in its own package (platforms/<name>/) exposing `create(settings) -> Platform`.
Add a new platform by creating that package and listing it in PLATFORMS.
"""

from __future__ import annotations

from importlib import import_module

from wingman.errors import ConfigError
from wingman.platforms.base import Platform

PLATFORMS = ("reddit", "bluesky", "mastodon")
# Which platform a pasted thread URL belongs to, so `wingman draft <url>` needs no --platform.
URL_HOSTS = {"reddit.com": "reddit", "www.reddit.com": "reddit", "old.reddit.com": "reddit",
             "redd.it": "reddit", "bsky.app": "bluesky"}


def platform_for_url(url: str) -> str | None:
    from urllib.parse import urlsplit

    from wingman.platforms.mastodon.api import WEB_STATUS_URL

    host = URL_HOSTS.get(urlsplit(url).netloc.lower())
    if host:
        return host
    # Mastodon runs on thousands of hosts; recognise its status-link shape instead.
    return "mastodon" if WEB_STATUS_URL.match(url) else None


def get_platform(name: str, settings: dict) -> Platform:
    if name not in PLATFORMS:
        raise ConfigError(f"Unknown platform '{name}'. Available: {', '.join(PLATFORMS)}")
    return import_module(f"wingman.platforms.{name}").create(settings)
