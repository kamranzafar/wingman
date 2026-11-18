"""Thin wrapper around the Claude API for structured (Pydantic) outputs, with cost tracking."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from wingman.core.models import now_utc
from wingman.core.usage import PRICES, UsageEntry, UsageLog, estimate_cost, short_model_name
from wingman.errors import LLMError

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that accept server-side refusal fallbacks (Haiku doesn't).
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5-2"}

__all__ = ["LLM", "LLMError"]


class LLM:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium",
                 task_models: dict[str, str] | None = None, usage_log: UsageLog | None = None,
                 on_usage: Callable[[str], None] | None = None, client: anthropic.Anthropic | None = None):
        self._client = client
        self.model = model
        self.effort = effort
        self.task_models = task_models or {}  # e.g. {"extract": "claude-haiku-5-5"}
        self.usage_log = usage_log
        self.on_usage = on_usage              # e.g. print a one-line cost summary

    @property
    def client(self) -> anthropic.Anthropic:
        # Created on first use, so commands that never call Claude don't need credentials.
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def model_for(self, task: str | None) -> str:
        return self.task_models.get(task, self.model) if task else self.model

    def parse(self, *, system: str, prompt: str, schema: type[T], effort: str | None = None,
              max_tokens: int = 16000, task: str | None = None, cache: bool = False) -> T:
        """One request -> a validated Pydantic object.

        `task` picks a per-task model (falls back to `model`). Keep `system` stable and put
        per-request data in `prompt`. `cache=True` caches `system` for ~5 minutes: writing it
        costs 1.25x, each reuse 0.1x, so only cache when several calls share it in quick
        succession (ranking batches, a review session), not for one-off requests.
        """
        model = self.model_for(task)
        extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
        system_block = {"type": "text", "text": system}
        if cache:
            system_block["cache_control"] = {"type": "ephemeral"}
        try:
            # Streamed so long outputs (e.g. profile builds) don't hit the SDK's
            # non-streaming time limit; we only need the final, parsed message.
            with self.client.beta.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=[system_block],
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": effort or self.effort},
                **extra,
            ) as stream:
                response = stream.get_final_message()
        except anthropic.AuthenticationError as e:
            raise LLMError("Claude API authentication failed. Set ANTHROPIC_API_KEY in .env "
                           "or run `ant auth login`.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Claude API rate limit hit; try again in a minute.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Claude API (network error).") from e

        self._record(task, model, response)  # billed even if the result is unusable below
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError("Response was cut off (max_tokens); try a smaller batch.")
        if response.parsed_output is None:
            raise LLMError("Claude returned no structured output.")
        return response.parsed_output

    def _record(self, task: str | None, requested: str, response) -> None:
        served = getattr(response, "model", None) or requested  # differs if a fallback model answered
        usage = response.usage
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cost = estimate_cost(served if served in PRICES else requested, usage.input_tokens, usage.output_tokens,
                             cache_write, cache_read)
        entry = UsageEntry(at=now_utc().isoformat(), task=task or "default", model=served,
                           input=usage.input_tokens, cache_write=cache_write, cache_read=cache_read,
                           output=usage.output_tokens, cost=round(cost, 6))
        if self.usage_log:
            self.usage_log.append(entry)
        if self.on_usage:
            self.on_usage(f"{short_model_name(served)} · {entry.total_input / 1000:.1f}K in / "
                          f"{entry.output / 1000:.1f}K out · ${cost:.3f}")
