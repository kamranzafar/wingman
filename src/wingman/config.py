# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Typed configuration: wingman.toml + .env, plus the data/ directory, all in one "home" directory.

Home is, in order: $WINGMAN_HOME; the current directory if it has a wingman.toml; your user config
directory (~/.config/wingman, or $XDG_CONFIG_HOME/wingman) if it has one; else the current directory.

Every default lives here, once. Unknown keys and invalid values are reported instead of ignored
(platform sections are checked by the platform registry, since each adapter has its own options).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from wingman.errors import ConfigError

Effort = Literal["low", "medium", "high", "xhigh", "max"]


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileSettings(_Section):
    blog_url: str = ""
    blog_max_pages: int = Field(15, ge=1)
    link_blog: Literal["never", "when_directly_relevant"] = "when_directly_relevant"
    pages: list[str] = []         # background pages read as-is (homepage, project sites)
    files: list[str] = []         # local .pdf/.docx/.md/.txt (LinkedIn export, CV)
    github: str = ""              # username
    github_max_repos: int = Field(10, ge=1, le=100)
    private_terms: list[str] = [] # never sent to the drafting model; posting blocked if present


class LLMSettings(_Section):
    model: str = "claude-opus-5-5"            # default for tasks without their own model
    rank_model: str = "claude-haiku-5-5"      # scoring many threads per scan
    profile_model: str = "claude-sonnet-5-5"
    extract_model: str = "claude-haiku-5-5"
    draft_model: str = "claude-sonnet-5-5"
    resume_model: str = "claude-sonnet-5-5"   # tailored resumes and cover letters (`wingman jobs`)
    effort: Effort = "medium"

    def task_models(self) -> dict[str, str]:
        return {"profile": self.profile_model, "extract": self.extract_model, "draft": self.draft_model,
                "rank": self.rank_model, "resume": self.resume_model}


class DraftingSettings(_Section):
    drafts: int = Field(2, ge=1, le=4)
    excerpts: int = Field(4, ge=0, le=12)
    embed_model: str = "nomic-embed-text"     # "none" = keyword search only
    index_max_posts: int = Field(50, ge=1)
    recency_half_life_years: float = Field(5.0, gt=0)
    recency_floor: float = Field(0.6, ge=0, le=1)
    project_boost: float = Field(1.2, ge=1)
    rank_fusion_k: int = Field(10, ge=1)
    max_draft_chars: int = Field(1500, ge=200, le=10_000)  # comments, not articles; enforced at posting too
    ask_rating: bool = True  # rate drafts 1-5 after each thread; see `wingman quality`


class PacingSettings(_Section):
    max_per_hour: int = Field(3, ge=1)
    max_per_day: int = Field(12, ge=1)
    max_per_source_per_day: int = Field(3, ge=1)
    min_gap_minutes: int = Field(10, ge=0)


class JobSearch(_Section):
    """One saved job search (Adzuna). Only `what` is required."""
    what: str = Field(min_length=1)            # keywords, e.g. "senior software engineer kafka"
    where: str = ""                            # city, region or postcode
    exclude: list[str] = []                    # drop ads mentioning these words
    salary_min: int = Field(0, ge=0)
    full_time: bool = False
    permanent: bool = False


class JobsSettings(_Section):
    base_resume: str = ""                      # master CV (.docx/.md/.txt/.pdf): the only source of facts
    country: str = Field("au", pattern=r"^[a-z]{2}$")  # Adzuna country code
    searches: list[JobSearch] = []
    exclude_companies: list[str] = []
    max_days_old: int = Field(7, ge=1, le=60)
    per_search_limit: int = Field(50, ge=1, le=200)
    min_fit: int = Field(6, ge=0, le=10)       # `jobs review` shows jobs at or above this fit
    output_dir: str = "applications"           # exported resumes and cover letters, one folder per job


class Settings(_Section):
    default_platform: str = ""    # used when a command gets no --platform; else the first enabled platform
    profile: ProfileSettings = ProfileSettings()
    llm: LLMSettings = LLMSettings()
    drafting: DraftingSettings = DraftingSettings()
    pacing: PacingSettings = PacingSettings()
    jobs: JobsSettings = JobsSettings()
    # Each platform adapter validates its own section, so these stay free-form here.
    platforms: dict[str, dict] = {}


@dataclass
class Config:
    home: Path
    settings: Settings

    @cached_property
    def data_dir(self) -> Path:
        d = self.home / "data"
        d.mkdir(exist_ok=True)
        return d

    def platform_settings(self, name: str) -> dict:
        settings = self.settings.platforms.get(name)
        if not settings:
            raise ConfigError(f"No [platforms.{name}] section in wingman.toml")
        if not settings.get("enabled", True):
            raise ConfigError(f"Platform '{name}' is disabled in wingman.toml")
        return settings

    def platform_name(self, name: str | None) -> str:
        """--platform if given, else default_platform, else the first enabled [platforms.*] section."""
        if name:
            return name
        if self.settings.default_platform:
            return self.settings.default_platform
        enabled = [n for n, s in self.settings.platforms.items() if s.get("enabled", True)]
        if not enabled and self.settings.platforms:
            raise ConfigError("No platform enabled: set enabled = true under the [platforms.*] sections you use "
                              "in wingman.toml, or pass --platform.")
        if not enabled:
            raise ConfigError("No platform configured: add a [platforms.<name>] section to wingman.toml "
                              "(see wingman.example.toml), or pass --platform.")
        return enabled[0]

    def resolve(self, path: str) -> Path:
        """A path from the config, relative to the wingman home directory."""
        p = Path(path).expanduser()
        return p if p.is_absolute() else self.home / p


def user_config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "wingman"


def find_home() -> Path:
    if os.environ.get("WINGMAN_HOME"):
        return Path(os.environ["WINGMAN_HOME"]).expanduser()
    cwd = Path.cwd()
    if (cwd / "wingman.toml").exists():
        return cwd
    user = user_config_dir()
    return user if (user / "wingman.toml").exists() else cwd


def load_config(home: Path | None = None) -> Config:
    home = home or find_home()
    load_dotenv(home / ".env")
    path = home / "wingman.toml"
    try:
        raw = tomllib.loads(path.read_text()) if path.exists() else {}
        return Config(home=home, settings=Settings.model_validate(raw))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"wingman.toml isn't valid TOML: {e}") from e
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors())
        raise ConfigError(f"wingman.toml has invalid settings: {problems}") from e
