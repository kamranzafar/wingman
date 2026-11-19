# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""One place for outbound HTTP: certifi trust store (python.org macOS builds ship without one),
a descriptive User-Agent, and timeouts."""

from __future__ import annotations

import json
import ssl
import urllib.request
from typing import Any

import certifi

from wingman import __version__

USER_AGENT = f"wingman/{__version__} (personal reader)"
_SSL = ssl.create_default_context(cafile=certifi.where())


def get_bytes(url: str, headers: dict[str, str] | None = None, timeout: float = 20) -> tuple[bytes, str | None]:
    """GET a URL. Returns (body, charset). Raises urllib.error.HTTPError / URLError."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read(), r.headers.get_content_charset()


def get_text(url: str, headers: dict[str, str] | None = None, timeout: float = 20) -> str:
    body, charset = get_bytes(url, headers, timeout)
    return body.decode(charset or "utf-8", errors="replace")


def get_json(url: str, headers: dict[str, str] | None = None, timeout: float = 20) -> Any:
    body, _ = get_bytes(url, headers, timeout)
    return json.loads(body)


def post_json(url: str, payload: Any, headers: dict[str, str] | None = None, timeout: float = 120) -> Any:
    """POST JSON, return the decoded JSON response."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
        "Content-Type": "application/json", "User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL if url.startswith("https") else None) as r:
        return json.loads(r.read())
