"""Bluesky: open AT Protocol API, so wingman can scan, draft and post (after you approve each reply).

Credentials: BLUESKY_HANDLE and BLUESKY_APP_PASSWORD in .env (an app password, never your main one).
"""

from __future__ import annotations


def create(settings: dict):
    from wingman.platforms.bluesky.api import BlueskyPlatform

    return BlueskyPlatform(settings)
