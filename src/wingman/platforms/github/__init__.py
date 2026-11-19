# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""GitHub Discussions: GraphQL API, so wingman can scan, draft and post (after you approve each reply).

Credentials: GITHUB_TOKEN in .env (a classic token with the `public_repo` scope), or a logged-in `gh` CLI.
"""

from __future__ import annotations

SETTINGS = frozenset({"skip_answered"})  # keys beyond platforms.COMMON


def create(settings: dict):
    from wingman.platforms.github.api import GitHubDiscussionsPlatform

    return GitHubDiscussionsPlatform(settings)
