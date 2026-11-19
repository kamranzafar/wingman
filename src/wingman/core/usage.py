# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Claude API cost tracking: pricing, a JSONL usage log, and summaries for `wingman cost`."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

# USD per million tokens: (input, output, cache read). 5-minute cache writes bill at 1.25x input.
# Estimates from published list prices; the Claude Console is the source of truth.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-fable-5-2": (8.00, 40.00, 0.20),
}
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class UsageEntry:
    at: str
    task: str
    model: str
    input: int
    cache_write: int
    cache_read: int
    output: int
    cost: float

    @property
    def total_input(self) -> int:
        return self.input + self.cache_write + self.cache_read


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  cache_write: int = 0, cache_read: int = 0) -> float:
    price_in, price_out, price_cache = PRICES.get(model, PRICES["claude-opus-5-5"])
    return (input_tokens * price_in + cache_write * price_in * CACHE_WRITE_MULTIPLIER
            + cache_read * price_cache + output_tokens * price_out) / 1_000_000


def short_model_name(model: str) -> str:
    """claude-sonnet-5-5 -> Sonnet 5.5"""
    name, *version = model.removeprefix("claude-").split("-")
    return f"{name.title()} {'.'.join(version)}".strip()


class UsageLog:
    def __init__(self, path: Path):
        self.path = path

    def append(self, entry: UsageEntry) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(asdict(entry)) + "\n")

    def entries(self) -> list[UsageEntry]:
        if not self.path.exists():
            return []
        return [UsageEntry(**json.loads(line)) for line in self.path.read_text().splitlines() if line.strip()]


@dataclass
class TaskSummary:
    task: str
    model: str
    requests: int
    avg_input: float
    avg_output: float
    total_cost: float


@dataclass
class Summary:
    today: float
    month: float
    all_time: float
    requests: int
    by_task: list[TaskSummary]


def summarize(entries: list[UsageEntry], now: datetime) -> Summary:
    def local(e: UsageEntry) -> datetime:
        return datetime.fromisoformat(e.at).astimezone(now.tzinfo)

    groups: dict[tuple[str, str], list[UsageEntry]] = defaultdict(list)
    for e in entries:
        groups[(e.task, e.model)].append(e)
    by_task = [
        TaskSummary(task, model, len(rows), sum(r.total_input for r in rows) / len(rows),
                    sum(r.output for r in rows) / len(rows), sum(r.cost for r in rows))
        for (task, model), rows in groups.items()
    ]
    return Summary(
        today=sum(e.cost for e in entries if local(e).date() == now.date()),
        month=sum(e.cost for e in entries if (local(e).year, local(e).month) == (now.year, now.month)),
        all_time=sum(e.cost for e in entries),
        requests=len(entries),
        by_task=sorted(by_task, key=lambda s: -s.total_cost),
    )
