# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Hacker News: reads the public Algolia HN Search API; drafts replies and posts that you edit and submit.

HN has no posting API: wingman copies your final text and opens the thread (or HN's submit form for a
new post), and you paste and submit it.
No credentials. HN_USERNAME in .env (optional) skips your own stories and lets `stats` read your comments.
"""

from __future__ import annotations

SETTINGS = frozenset({"min_points"})  # keys beyond platforms.COMMON


def create(settings: dict):
    from wingman.platforms.hackernews.api import HackerNewsPlatform

    return HackerNewsPlatform(settings)
