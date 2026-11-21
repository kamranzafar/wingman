# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Scores job ads against your master CV: fit 0-10, what to lead with, and the gaps."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel

from wingman.core.llm import LLM
from wingman.jobs.models import Job, RankedJob

BATCH = 20
CV_CHARS = 15_000

SYSTEM_TEMPLATE = """You help one specific person decide which job ads are worth applying for. \
Score each ad 0-10 for how well THEIR experience (master CV below) matches what the role needs.

10 = they meet every key requirement and the seniority fits.
7-9 = strong match; a gap or two that a tailored resume can address honestly.
4-6 = partial match; several requirements they can't show.
0-3 = wrong field, seniority or location, or a recruiter ad with no real role in it.

Judge only from the CV and the ad. Seniority and the core tech/domain matter more than keyword overlap.
For each ad:
- angle: the one strength from their CV to lead with for this role, written to them ("lead with your…"), \
at most ~20 words.
- gaps: up to 3 requirements the CV doesn't show, short. Empty if none.

{criteria}
MASTER CV
{cv}

PROFILE (interests and voice)
{profile}"""


class JobScore(BaseModel):
    index: int
    fit: int
    angle: str
    gaps: list[str]


class JobScores(BaseModel):
    scores: list[JobScore]


def rank_jobs(llm: LLM, cv: str, profile_brief: str, jobs: list[Job], criteria: str = "",
              progress: Callable[[str], None] = lambda _: None) -> list[RankedJob]:
    system = SYSTEM_TEMPLATE.format(cv=cv[:CV_CHARS], profile=profile_brief,
                                    criteria=f"What they're looking for: {criteria}\n" if criteria else "")
    out: list[RankedJob] = []
    for start in range(0, len(jobs), BATCH):
        batch = jobs[start:start + BATCH]
        progress(f"ranking {start + 1}-{start + len(batch)} of {len(jobs)}")
        prompt = "\n\n".join(f"[{i}]\n{j.as_text()[:1500]}" for i, j in enumerate(batch))
        result = llm.parse(system=system, prompt=prompt, schema=JobScores, effort="low", task="rank",
                           cache=len(jobs) > BATCH)  # batches share the (CV-sized) system prompt
        by_index = {s.index: s for s in result.scores}
        for i, job in enumerate(batch):
            s = by_index.get(i)
            out.append(RankedJob(job, max(0, min(10, s.fit)) if s else 0, s.angle if s else "",
                                 (s.gaps[:3] if s else [])))
    return out
