"""Karma analytics over your own comment history, plus an optional Claude-written 'what works' note."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from statistics import mean, median

from pydantic import BaseModel
from rich.table import Table

from wingman.core.drafter import learnings_path
from wingman.core.llm import LLM
from wingman.core.models import ActivityItem
from wingman.ui import console

LENGTH_BUCKETS = [(0, 100, "< 100 chars"), (100, 300, "100-300"), (300, 800, "300-800"), (800, 10**9, "800+")]


def _table(title: str, rows: list[tuple], headers: list[str]):
    t = Table(title=title, title_justify="left")
    for h in headers:
        t.add_column(h, justify="right" if h != headers[0] else "left")
    for r in rows:
        t.add_row(*[str(x) for x in r])
    console.print(t)


def _group(items: list[ActivityItem], key) -> list[tuple]:
    groups: dict = defaultdict(list)
    for i in items:
        groups[key(i)].append(i.score)
    return [(k, len(v), sum(v), f"{mean(v):.1f}", f"{median(v):.0f}", max(v)) for k, v in groups.items()]


def show_activity(items: list[ActivityItem]):
    """For sources without vote counts (RSS): what you posted, where and when."""
    if not items:
        console.print("No comments found yet.")
        return
    console.print(f"[bold]{len(items)} recent comments[/] [dim](karma scores need Reddit API access)[/]")
    for title, column, key in (("By community", "community", lambda i: i.source),
                               ("By hour posted (local time)", "hour",
                                lambda i: f"{i.created_at.astimezone().hour:02d}:00")):
        counts: dict = defaultdict(int)
        for i in items:
            counts[key(i)] += 1
        rows = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
        _table(title, rows, [column, "comments"])


def show_stats(items: list[ActivityItem], wingman_urls: set[str]):
    if not items:
        console.print("No comments found yet.")
        return
    scores = [i.score for i in items]
    console.print(f"[bold]{len(items)} comments[/] · total {sum(scores)} · avg {mean(scores):.1f} · "
                  f"median {median(scores):.0f}")
    headers = ["", "comments", "total", "avg", "median", "best"]

    by_source = sorted(_group(items, lambda i: i.source), key=lambda r: float(r[3]), reverse=True)
    _table("By community", by_source[:15], ["community", *headers[1:]])

    by_hour = sorted(_group(items, lambda i: i.created_at.astimezone().hour))
    _table("By hour posted (local time)", [(f"{h:02d}:00", *rest) for h, *rest in by_hour], ["hour", *headers[1:]])

    def bucket(i):
        return next(label for lo, hi, label in LENGTH_BUCKETS if lo <= len(i.body) < hi)

    order = [b[2] for b in LENGTH_BUCKETS]
    by_len = sorted(_group(items, bucket), key=lambda r: order.index(r[0]))
    _table("By length", by_len, ["length", *headers[1:]])

    w = [i for i in items if i.permalink in wingman_urls]
    if w:
        console.print(f"[bold]Posted via wingman:[/] {len(w)} comments, avg {mean(i.score for i in w):.1f}")

    top = sorted(items, key=lambda i: i.score, reverse=True)[:5]
    _table("Top comments", [(i.source, i.score, i.body[:80].replace("\n", " ")) for i in top],
           ["community", "score", "excerpt"])


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
