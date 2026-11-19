# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Mastodon: open REST API on your own instance, so wingman can scan, draft and post (after you approve each reply).

Settings: `instance` in [platforms.mastodon]. Credentials: MASTODON_ACCESS_TOKEN in .env, created on your
instance under Preferences → Development → New application, with scopes `read` and `write:statuses`.
"""

from __future__ import annotations

# keys beyond platforms.COMMON
SETTINGS = frozenset({"instance", "languages", "visibility", "max_characters", "include_bots"})


def create(settings: dict):
    from wingman.platforms.mastodon.api import MastodonPlatform

    return MastodonPlatform(settings)
