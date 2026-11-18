"""Your public GitHub repos and READMEs, with a local cache for when the API rate-limits."""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import asdict
from pathlib import Path

from wingman.core import http
from wingman.core.models import Document

log = logging.getLogger(__name__)

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json"}
README_CHARS = 5000


def _repo_document(username: str, repo: dict) -> Document:
    readme = ""
    try:
        data = http.get_json(f"{API}/repos/{username}/{repo['name']}/readme", HEADERS)
        readme = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - repo without a README
        pass
    meta = (f"Language: {repo.get('language') or '-'} · Stars: {repo['stargazers_count']} · "
            f"Topics: {', '.join(repo.get('topics') or []) or '-'} · Last push: {repo['pushed_at'][:10]}"
            + (f" · Site: {repo['homepage']}" if repo.get("homepage") else ""))
    return Document(title=f"GitHub repo: {repo['name']}", url=repo["html_url"], kind="github",
                    text=f"{repo.get('description') or ''}\n{meta}\n\n{readme[:README_CHARS]}",
                    date=repo["pushed_at"][:10])


def fetch_github(username: str, max_repos: int) -> tuple[Document, list[Document]]:
    """(profile, repos): public, non-fork, non-archived repos, most recently pushed first.

    Uses GitHub's unauthenticated API (60 requests/hour).
    """
    user = http.get_json(f"{API}/users/{username}", HEADERS)
    repos = [r for r in http.get_json(f"{API}/users/{username}/repos?per_page=100&sort=pushed", HEADERS)
             if not r["fork"] and not r["archived"]]
    profile = Document(title=f"GitHub profile: {username}", url=user["html_url"], kind="github",
                       text=f"{user.get('name') or username}. {user.get('bio') or ''}\n"
                            f"{user['public_repos']} public repos, {user['followers']} followers.")
    return profile, [_repo_document(username, r) for r in repos[:max_repos]]


class GitHubSource:
    """Fetches repos, caching the result so a rate-limited run can fall back to the last good copy."""

    def __init__(self, username: str, max_repos: int, cache_path: Path):
        self.username = username
        self.max_repos = max_repos
        self.cache_path = cache_path

    def load(self) -> tuple[list[Document], str]:
        """(documents including the profile, note on where they came from)."""
        try:
            profile, repos = fetch_github(self.username, self.max_repos)
        except Exception as e:  # rate limit, network: fall back to the last good copy
            cached = self._read_cache()
            if cached is None:
                raise
            return cached, f"GitHub unavailable ({e}); using {len(cached) - 1} cached READMEs"
        docs = [profile, *repos]
        self.cache_path.write_text(json.dumps([asdict(d) for d in docs]))
        return docs, f"{len(repos)} GitHub READMEs"

    def _read_cache(self) -> list[Document] | None:
        if not self.cache_path.exists():
            return None
        return [Document(**d) for d in json.loads(self.cache_path.read_text())]
