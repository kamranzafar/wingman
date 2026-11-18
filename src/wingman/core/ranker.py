"""Scores threads: Claude judges relevance to your profile; a local formula judges opportunity."""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime

from pydantic import BaseModel

from wingman.core.llm import LLM
from wingman.core.models import RankedThread, Thread

BATCH = 20
RELEVANCE_WEIGHT = 0.7  # final = 0.7 * relevance + 0.3 * opportunity

SYSTEM_TEMPLATE = """You help one specific person decide which online threads are worth their \
time to reply to. Score each thread 0-10 for how well THIS person could add a genuinely useful, \
non-obvious reply, based on their profile below.

10 = squarely in an "expert" topic and the question needs exactly their knowledge.
6-8 = solid fit; they could add something real.
3-5 = loosely related; a reply would be generic.
0-2 = off-topic, a meme/rant/low-effort post, or not something to wade into.

Bonus for {keywords}.
For each thread give a one-line `angle`: the specific thing they could contribute.

PROFILE
{profile}"""


class Score(BaseModel):
    index: int
    relevance: int
    angle: str


class Scores(BaseModel):
    scores: list[Score]


def opportunity(thread: Thread, now: datetime) -> float:
    """0-1. Fresh, gaining traction, and not yet crowded = high."""
    age = thread.age_hours(now)
    freshness = math.exp(-age / 6)                         # half-ish life ~4h
    velocity = min(max(thread.score, 0) / age / 20, 1.0)   # 20 upvotes/hour saturates
    room = 1 / (1 + thread.num_comments / 15)              # fewer comments = more visibility
    return round(0.45 * freshness + 0.25 * velocity + 0.30 * room, 3)


def rank(llm: LLM, profile_brief: str, keywords: list[str], threads: list[Thread], now: datetime,
         progress: Callable[[str], None] = lambda _: None) -> list[RankedThread]:
    system = SYSTEM_TEMPLATE.format(
        profile=profile_brief,
        keywords=("threads mentioning: " + ", ".join(keywords)) if keywords else "nothing in particular",
    )
    out: list[RankedThread] = []
    for start in range(0, len(threads), BATCH):
        batch = threads[start:start + BATCH]
        lines = [f"[{i}] {t.source} | {t.num_comments} comments | {t.age_hours(now):.1f}h old\n"
                 f"Title: {t.title}\n{t.body[:700]}\n" for i, t in enumerate(batch)]
        progress(f"ranking {start + 1}-{start + len(batch)} of {len(threads)}")
        result = llm.parse(system=system, prompt="\n".join(lines), schema=Scores, effort="low", task="rank",
                           cache=len(threads) > BATCH)  # several batches share the system prompt
        by_index = {s.index: s for s in result.scores}
        for i, t in enumerate(batch):
            s = by_index.get(i)
            rel = max(0, min(10, s.relevance)) if s else 0
            opp = opportunity(t, now)
            final = round(RELEVANCE_WEIGHT * rel / 10 + (1 - RELEVANCE_WEIGHT) * opp, 3)
            out.append(RankedThread(t, rel, opp, final, s.angle if s else ""))
    return out
