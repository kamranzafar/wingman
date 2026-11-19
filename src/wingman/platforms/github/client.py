# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Minimal GitHub GraphQL client: a token from GITHUB_TOKEN or the gh CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
from typing import Any

from wingman.core import http
from wingman.errors import ConfigError, WingmanError

ENDPOINT = "https://api.github.com/graphql"
TOKEN_HELP = ("set GITHUB_TOKEN in .env (github.com → Settings → Developer settings → Personal access tokens → "
              "Tokens (classic), scope `public_repo`), or log in with `gh auth login`")


class GitHubError(WingmanError):
    pass


def find_token() -> str:
    """GITHUB_TOKEN, else the token of a logged-in gh CLI, else ''."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token or not shutil.which("gh"):
        return token
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


class GraphQLClient:
    def __init__(self, token: str):
        if not token:
            raise ConfigError(f"No GitHub token: {TOKEN_HELP}.")
        self._token = token
        self._viewer: str | None = None

    def query(self, query: str, **variables: Any) -> dict:
        try:
            out = http.post_json(ENDPOINT, {"query": query, "variables": variables},
                                 headers={"Authorization": f"bearer {self._token}"}, timeout=30)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise ConfigError(f"GitHub rejected the token: {TOKEN_HELP}.") from e
            try:
                detail = json.loads(e.read()).get("message", e.reason)
            except Exception:  # noqa: BLE001 - non-JSON error page
                detail = e.reason
            raise GitHubError(f"GitHub API failed (HTTP {e.code}): {detail}") from e
        if out.get("errors"):
            messages = "; ".join(err.get("message", "?") for err in out["errors"])
            if any(err.get("type") == "FORBIDDEN" for err in out["errors"]):
                messages += " (does the token have the `public_repo` scope?)"
            raise GitHubError(f"GitHub API: {messages}")
        return out["data"]

    @property
    def viewer(self) -> str:
        """Your login, fetched once."""
        if self._viewer is None:
            self._viewer = self.query("query { viewer { login } }")["viewer"]["login"]
        return self._viewer
