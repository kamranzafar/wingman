# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Collecting your own writing and background, shared by `profile build` and `index`.

Each source is optional and fails independently: a skipped source is reported, not fatal.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from wingman.config import Config
from wingman.core.models import Document, now_utc
from wingman.sources.files import read_file
from wingman.sources.github import GitHubSource
from wingman.sources.web import fetch_blog, fetch_page

Progress = Callable[[str], None]


@dataclass
class Collected:
    blog: list[Document] = field(default_factory=list)
    background: list[Document] = field(default_factory=list)  # pages, projects, GitHub, files
    skipped: list[str] = field(default_factory=list)

    @property
    def all(self) -> list[Document]:
        return self.blog + self.background


def collect(cfg: Config, *, max_posts: int, include_files: bool, progress: Progress = lambda _: None) -> Collected:
    """Blog posts, GitHub repos, background pages and (optionally) local files from wingman.toml.

    `include_files` is off for the writing index: LinkedIn exports and CVs inform the profile,
    but their formal register and employer details don't belong in reply drafts.
    """
    p = cfg.settings.profile
    out = Collected()

    if p.blog_url:
        try:
            out.blog = fetch_blog(p.blog_url, max_posts)
            progress(f"{len(out.blog)} blog posts")
        except Exception as e:  # noqa: BLE001
            out.skipped.append(f"blog: {e}")

    if p.github:
        try:
            docs, note = GitHubSource(p.github, p.github_max_repos, cfg.data_dir / "github.json").load()
            out.background += docs
            progress(note)
        except Exception as e:  # noqa: BLE001
            out.skipped.append(f"GitHub: {e}")

    home_host = urlsplit(p.blog_url).netloc
    today = now_utc().date().isoformat()
    for url in p.pages:
        # Pages on your own site describe you; pages elsewhere are your projects (e.g. a product site).
        kind = "page" if urlsplit(url).netloc == home_host else "project"
        try:
            out.background.append(fetch_page(url, kind=kind, date=today))
            progress(f"{url} ({kind})")
        except Exception as e:  # noqa: BLE001
            out.skipped.append(f"{url}: {e}")

    if include_files:
        for name in p.files:
            path = cfg.resolve(name)
            try:
                out.background.append(read_file(path))
                progress(path.name)
            except FileNotFoundError:
                out.skipped.append(f"{name}: file not found")
            except Exception as e:  # noqa: BLE001
                out.skipped.append(f"{name}: {e}")
    return out
