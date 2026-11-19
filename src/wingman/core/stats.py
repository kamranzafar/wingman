# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Karma analytics over your own comment history, plus an optional Claude-written 'what works' note."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, median

from pydantic import BaseModel

from wingman.core.drafter import learnings_path
from wingman.core.llm import LLM
from wingman.core.models import ActivityItem

LENGTH_BUCKETS = [(0, 100, "< 100 chars"), (100, 300, "100-300"), (300, 800, "300-800"), (800, 10**9, "800+")]


def _group(items: list[ActivityItem], key) -> list[tuple]:
    groups: dict = defaultdict(list)
    for i in items:
        groups[key(i)].append(i.score)
    return [(k, len(v), sum(v), f"{mean(v):.1f}", f"{median(v):.0f}", max(v)) for k, v in groups.items()]


@dataclass
class Breakdown:
    """One table: a title, column headers, and rows of display-ready values."""

    title: str
    headers: list[str]
    rows: list[tuple]


@dataclass
class StatsSummary:
    count: int
    total: int
    average: float
    median: float
    breakdowns: list[Breakdown]
    wingman: tuple[int, float] | None  # (posts, average score) for replies posted through wingman
    top: list[ActivityItem]


def activity_breakdowns(items: list[ActivityItem]) -> list[Breakdown]:
    """For sources without vote counts (RSS): how many posts by community and by hour."""
    out = []
    for title, column, key in (("By community", "community", lambda i: i.source),
                               ("By hour posted (local time)", "hour",
                                lambda i: f"{i.created_at.astimezone().hour:02d}:00")):
        counts: dict = defaultdict(int)
        for i in items:
            counts[key(i)] += 1
        out.append(Breakdown(title, [column, "comments"], sorted(counts.items(), key=lambda kv: kv[1], reverse=True)))
    return out


def summarize(items: list[ActivityItem], wingman_urls: set[str]) -> StatsSummary | None:
    """Score analytics by community, hour and length; None when there's nothing to summarise."""
    if not items:
        return None
    scores = [i.score for i in items]
    cols = ["comments", "total", "avg", "median", "best"]

    by_source = sorted(_group(items, lambda i: i.source), key=lambda r: float(r[3]), reverse=True)[:15]
    by_hour = [(f"{h:02d}:00", *rest) for h, *rest in sorted(_group(items, lambda i: i.created_at.astimezone().hour))]

    def bucket(i):
        return next(label for lo, hi, label in LENGTH_BUCKETS if lo <= len(i.body) < hi)

    order = [b[2] for b in LENGTH_BUCKETS]
    by_len = sorted(_group(items, bucket), key=lambda r: order.index(r[0]))

    w = [i.score for i in items if i.permalink in wingman_urls]
    return StatsSummary(
        count=len(items), total=sum(scores), average=mean(scores), median=median(scores),
        breakdowns=[Breakdown("By community", ["community", *cols], by_source),
                    Breakdown("By hour posted (local time)", ["hour", *cols], by_hour),
                    Breakdown("By length", ["length", *cols], by_len)],
        wingman=(len(w), mean(w)) if w else None,
        top=sorted(items, key=lambda i: i.score, reverse=True)[:5],
    )


class Learnings(BaseModel):
    patterns_that_work: list[str]
    patterns_to_avoid: list[str]
    best_communities: list[str]
    best_posting_times: str


def write_learnings(llm: LLM, items: list[ActivityItem], data_dir: Path) -> Path:
    ranked = sorted(items, key=lambda i: i.score, reverse=True)
    top, bottom = ranked[:25], ranked[-25:]

    def fmt(group):
        return "\n".join(f"- [{i.source}, {i.score} pts, {i.created_at.astimezone():%a %H:%M}] {i.body[:500]}"
                         for i in group)

    result = llm.parse(
        system="You analyse one person's comment history to find what makes their comments do well. "
               "Be concrete and specific to their data; no generic social-media advice.",
        prompt=f"## Highest-scoring comments\n{fmt(top)}\n\n## Lowest-scoring comments\n{fmt(bottom)}",
        schema=Learnings,
        effort="high",
    )
    md = ["## Patterns that work"] + [f"- {p}" for p in result.patterns_that_work]
    md += ["", "## Patterns to avoid"] + [f"- {p}" for p in result.patterns_to_avoid]
    md += ["", "## Best communities"] + [f"- {c}" for c in result.best_communities]
    md += ["", f"## Best posting times\n{result.best_posting_times}"]
    path = learnings_path(data_dir)
    path.write_text("\n".join(md) + "\n")
    return path
