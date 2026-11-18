"""Minimal AT Protocol (XRPC) client: an app-password session plus GET/POST calls."""

from __future__ import annotations

import json
import urllib.error
from typing import Any
from urllib.parse import urlencode

from wingman.core import http
from wingman.errors import ConfigError, WingmanError

DEFAULT_SERVICE = "https://bsky.social"


class XrpcError(WingmanError):
    pass


def _error(e: urllib.error.HTTPError, method: str) -> XrpcError:
    try:
        body = json.loads(e.read())
        detail = f"{body.get('error', '')}: {body.get('message', '')}".strip(": ")
    except Exception:  # noqa: BLE001 - non-JSON error page
        detail = e.reason
    return XrpcError(f"Bluesky {method} failed (HTTP {e.code}): {detail}")


class XrpcClient:
    """Logs in lazily with your handle + app password; never stores them anywhere."""

    def __init__(self, identifier: str, app_password: str, service: str = DEFAULT_SERVICE):
        if not identifier or not app_password:
            raise ConfigError("Set BLUESKY_HANDLE and BLUESKY_APP_PASSWORD in .env "
                              "(create an app password in Bluesky: Settings → Privacy and security → App passwords).")
        self.service = service.rstrip("/")
        self._identifier = identifier
        self._password = app_password
        self._session: dict | None = None

    @property
    def session(self) -> dict:
        if self._session is None:
            try:
                self._session = http.post_json(f"{self.service}/xrpc/com.atproto.server.createSession",
                                               {"identifier": self._identifier, "password": self._password},
                                               timeout=20)
            except urllib.error.HTTPError as e:
                if e.code == 401:
                    raise ConfigError("Bluesky login failed: check BLUESKY_HANDLE and BLUESKY_APP_PASSWORD.") from e
                raise _error(e, "login") from e
        return self._session

    @property
    def did(self) -> str:
        return self.session["did"]

    @property
    def handle(self) -> str:
        return self.session["handle"]

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.session['accessJwt']}"}

    def get(self, method: str, **params: Any) -> dict:
        query = urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
        try:
            return http.get_json(f"{self.service}/xrpc/{method}?{query}", headers=self._auth())
        except urllib.error.HTTPError as e:
            raise _error(e, method) from e

    def post(self, method: str, body: dict) -> dict:
        try:
            return http.post_json(f"{self.service}/xrpc/{method}", body, headers=self._auth(), timeout=30)
        except urllib.error.HTTPError as e:
            raise _error(e, method) from e
