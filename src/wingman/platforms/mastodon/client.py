# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Minimal Mastodon REST client: a bearer token against your own instance."""

from __future__ import annotations

import json
import urllib.error
from typing import Any
from urllib.parse import urlencode

from wingman.core import http
from wingman.errors import ConfigError, WingmanError

TOKEN_HELP = ("create one on your instance: Preferences → Development → New application, "
              "scopes `read` and `write:statuses`, then copy 'Your access token' into .env")


class MastodonError(WingmanError):
    pass


class MastodonClient:
    """Calls /api/... on your instance. The token is only ever sent there."""

    def __init__(self, instance: str, token: str):
        if not instance:
            raise ConfigError('Set instance = "https://your.instance" in [platforms.mastodon] in wingman.toml.')
        if not token:
            raise ConfigError(f"Set MASTODON_ACCESS_TOKEN in .env ({TOKEN_HELP}).")
        self.instance = instance.rstrip("/") if "://" in instance else f"https://{instance.rstrip('/')}"
        self._token = token
        self._me: dict | None = None

    def _error(self, e: urllib.error.HTTPError, path: str) -> WingmanError:
        if e.code == 401:
            return ConfigError(f"Mastodon rejected the token for {self.instance}: check MASTODON_ACCESS_TOKEN "
                               f"({TOKEN_HELP}).")
        try:
            detail = json.loads(e.read()).get("error", e.reason)
        except Exception:  # noqa: BLE001 - non-JSON error page
            detail = e.reason
        if e.code == 403:
            detail = f"{detail} (does the token have the `read` and `write:statuses` scopes?)"
        return MastodonError(f"Mastodon {path} failed (HTTP {e.code}): {detail}")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    def get(self, path: str, **params: Any) -> Any:
        query = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        try:
            return http.get_json(f"{self.instance}{path}" + (f"?{query}" if query else ""), headers=self._headers())
        except urllib.error.HTTPError as e:
            raise self._error(e, path) from e

    def post(self, path: str, body: dict) -> Any:
        try:
            return http.post_json(f"{self.instance}{path}", body, headers=self._headers(), timeout=30)
        except urllib.error.HTTPError as e:
            raise self._error(e, path) from e

    @property
    def me(self) -> dict:
        """Your account (id, acct, url), fetched once."""
        if self._me is None:
            self._me = self.get("/api/v1/accounts/verify_credentials")
        return self._me
