"""Typed configuration: wingman.toml + .env, loaded from the working directory (or WINGMAN_HOME).

Every default lives here, once. Unknown keys and invalid values are reported instead of ignored.
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
    files: list[str] = []         # local .pdf/.md/.txt (LinkedIn export, CV)
    github: str = ""              # username
    github_max_repos: int = Field(10, ge=1, le=100)
    private_terms: list[str] = [] # never sent to the drafting model; posting blocked if present


class LLMSettings(_Section):
    model: str = "claude-opus-5-5"            # default for tasks without their own model
    rank_model: str = "claude-haiku-5-5"      # scoring many threads per scan
    profile_model: str = "claude-sonnet-5-5"
    extract_model: str = "claude-haiku-5-5"
    draft_model: str = "claude-sonnet-5-5"
    effort: Effort = "medium"

    def task_models(self) -> dict[str, str]:
        return {"profile": self.profile_model, "extract": self.extract_model, "draft": self.draft_model,
                "rank": self.rank_model}


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


class Settings(_Section):
    profile: ProfileSettings = ProfileSettings()
    llm: LLMSettings = LLMSettings()
    drafting: DraftingSettings = DraftingSettings()
    pacing: PacingSettings = PacingSettings()
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

    def resolve(self, path: str) -> Path:
        """A path from the config, relative to the wingman home directory."""
        p = Path(path).expanduser()
        return p if p.is_absolute() else self.home / p


def load_config(home: Path | None = None) -> Config:
    home = home or Path(os.environ.get("WINGMAN_HOME", Path.cwd()))
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
