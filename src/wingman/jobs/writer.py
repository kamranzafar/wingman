# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Drafts a tailored resume and cover letter for one job, strictly from your master CV."""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel

from wingman.core.drafter import PLACEHOLDER_MARKER
from wingman.core.llm import LLM
from wingman.core.retrieval import Index
from wingman.jobs.models import Job, RankedJob

CV_CHARS = 30_000

SYSTEM_TEMPLATE = """You tailor one specific person's job applications. For each job ad you write a \
resume and a cover letter that THEY will review, edit and send under their own name.

Their master CV below is the ONLY source of facts. Rules:
- Never invent or inflate anything: employers, titles, dates, degrees, certifications, technologies, \
team sizes, metrics or achievements must all come from the CV (or the excerpts from their own \
writing). Where the ad wants evidence the CV doesn't have, don't write around it: leave a \
placeholder like [your experience with X here], or leave it out and list it in `gaps`.
- Tailor by selecting, ordering and rephrasing: lead with the experience most relevant to this ad, \
cut what's irrelevant, and use the ad's own terms where they truthfully describe their work \
(applicant tracking systems match keywords).
- Resume (Markdown): `# Name`, a contact line exactly as in the CV, then `## Summary` (2-3 lines \
aimed at this role), `## Skills` (grouped, relevant first), `## Experience` (`### Title, Company` \
then a dates/location line, then 3-6 achievement bullets for recent roles, fewer for older ones), \
then education, certifications and projects as the CV supports. Australian conventions: "resume", \
no photo, date of birth, marital status or references ("available on request" is fine to omit); \
2-3 pages; Australian spelling.
- Cover letter (Markdown): about 250-350 words, addressed to the hiring manager (by name only if the \
ad gives one), in their own voice from the profile, plain and specific: why this role and company, \
2-3 concrete pieces of evidence from the CV mapped to what the ad asks for, and a short close. \
No clichés ("I am writing to express my interest", "passionate", "team player"), no flattery, \
no restating the whole resume.
- matched: the ad's key requirements, each with the CV evidence you used (short).
- gaps: requirements the CV doesn't show (so they can decide whether to apply or add something true).
- claims_to_verify: specific facts in the drafts worth double-checking (dates, numbers, names).

MASTER CV
{cv}

PROFILE (interests and writing voice)
{profile}"""


class Match(BaseModel):
    requirement: str
    evidence: str


class Application(BaseModel):
    resume_md: str
    cover_letter_md: str
    matched: list[Match]
    gaps: list[str]
    claims_to_verify: list[str]


@dataclass
class ApplicationResult:
    application: Application
    sources: list[str] = field(default_factory=list)
    model: str = ""


def placeholders(text: str) -> int:
    return text.count(PLACEHOLDER_MARKER)


class ApplicationWriter:
    def __init__(self, llm: LLM, cv: str, profile_brief: str, index: Index | None = None, excerpts: int = 3):
        self.llm, self.index, self.excerpts = llm, index, excerpts
        self.system = SYSTEM_TEMPLATE.format(cv=cv[:CV_CHARS], profile=profile_brief)

    def draft(self, job: Job, ranking: RankedJob | None = None, instruction: str = "",
              previous: Application | None = None) -> ApplicationResult:
        parts = [f"JOB AD\n{job.as_text()}"]
        if ranking and ranking.angle:
            parts.append(f"\nSuggested lead from triage: {ranking.angle}")
        sources: list[str] = []
        if self.index and self.excerpts:
            hits = self.index.search(f"{job.title}\n{job.description[:1500]}", k=self.excerpts)
            if hits:
                parts.append("\nExcerpts from their own writing (projects and specifics they can stand behind):")
                parts += [f"--- {h.source} ({h.kind})\n{h.text}" for h in hits]
                sources = list(dict.fromkeys(h.source for h in hits))
        if previous and instruction:
            parts.append(f"\nCURRENT RESUME\n{previous.resume_md}\n\nCURRENT COVER LETTER\n{previous.cover_letter_md}")
            parts.append(f"\nRevise both per this request, keeping everything else: {instruction}")
        elif instruction:
            parts.append(f"\nThe person asked for: {instruction}")
        # The CV-sized system prompt is cached: later jobs in a session (and rewrites) reuse it at ~0.1x.
        app = self.llm.parse(system=self.system, prompt="\n".join(parts), schema=Application, task="resume",
                             cache=True, max_tokens=32_000)
        return ApplicationResult(app, sources, self.llm.model_for("resume"))
