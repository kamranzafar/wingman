"""Builds the objects commands need from config (composition root). Created lazily, closed once."""

from __future__ import annotations

from collections.abc import Callable
from functools import cached_property

from wingman.config import Config, load_config
from wingman.core.drafter import Drafter, DraftingOptions, learnings_path
from wingman.core.llm import LLM
from wingman.core.retrieval import Index, OllamaEmbedder, Weighting
from wingman.core.store import Store
from wingman.core.usage import UsageLog
from wingman.platforms import get_platform
from wingman.platforms.base import Platform
from wingman.profile.voice import Profile, ProfileStore


class Services:
    def __init__(self, cfg: Config | None = None, on_usage: Callable[[str], None] | None = None):
        self.cfg = cfg or load_config()
        self._on_usage = on_usage

    @property
    def settings(self):
        return self.cfg.settings

    @cached_property
    def store(self) -> Store:
        return Store(self.cfg.data_dir / "wingman.db")

    @cached_property
    def usage_log(self) -> UsageLog:
        return UsageLog(self.cfg.data_dir / "usage.jsonl")

    @cached_property
    def llm(self) -> LLM:
        s = self.settings.llm
        return LLM(s.model, s.effort, task_models=s.task_models(), usage_log=self.usage_log, on_usage=self._on_usage)

    @cached_property
    def profiles(self) -> ProfileStore:
        return ProfileStore(self.cfg.data_dir)

    def profile(self) -> Profile:
        return self.profiles.load()

    def platform(self, name: str) -> Platform:
        return get_platform(name, self.cfg.platform_settings(name))

    # ------------------------------------------------------------ retrieval
    @property
    def index_path(self):
        return self.cfg.data_dir / "index.json"

    @property
    def weighting(self) -> Weighting:
        d = self.settings.drafting
        return Weighting(d.recency_half_life_years, d.recency_floor, d.project_boost, d.rank_fusion_k)

    def embedder(self) -> OllamaEmbedder | None:
        model = self.settings.drafting.embed_model
        return None if model.lower() == "none" else OllamaEmbedder(model)

    def index(self) -> Index | None:
        return Index.load(self.index_path, self.weighting)

    # ------------------------------------------------------------ drafting
    def max_post_chars(self, platform: Platform) -> int:
        return min(self.settings.drafting.max_draft_chars, platform.constraints.max_length)

    def drafter(self, platform: Platform, batch: bool = False) -> Drafter | None:
        """None when the platform doesn't allow AI drafts (you write the reply yourself).

        `batch=True` for sessions that draft several threads back to back, so the shared
        system prompt is cached between them; a single `wingman draft` doesn't cache.
        """
        if not platform.constraints.ai_drafts_allowed:
            return None
        d, p = self.settings.drafting, self.settings.profile
        lp = learnings_path(self.cfg.data_dir)
        options = DraftingOptions(link_policy=p.link_blog, num_drafts=d.drafts, excerpts=d.excerpts,
                                  private_terms=tuple(p.private_terms), max_chars=d.max_draft_chars,
                                  cache_prompt=batch)
        return Drafter(self.llm, self.profile().brief(), platform.constraints, options, index=self.index(),
                       learnings=lp.read_text() if lp.exists() else "")

    def composer(self, platform: Platform, article=None):
        """Drafts new standalone posts (topic or shared article) with the same options as replies."""
        from wingman.core.composer import Composer

        d, p = self.settings.drafting, self.settings.profile
        options = DraftingOptions(link_policy=p.link_blog, num_drafts=d.drafts, excerpts=d.excerpts,
                                  private_terms=tuple(p.private_terms), max_chars=d.max_draft_chars)
        return Composer(self.llm, self.profile().brief(), platform.constraints, options, index=self.index(),
                        article=article)

    def close(self) -> None:
        if "store" in self.__dict__:
            self.store.close()
