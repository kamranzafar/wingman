# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""A job ad, from Adzuna or pasted from a job site."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Literal

JobSource = Literal["adzuna", "paste"]


@dataclass
class Job:
    source: JobSource
    id: str
    title: str
    company: str
    location: str
    description: str
    url: str
    created_at: datetime
    salary_min: float | None = None
    salary_max: float | None = None
    salary_predicted: bool = False   # Adzuna's estimate, not the advertised salary
    salary_text: str = ""            # as written in a pasted ad ("$150k + super")
    work_type: str = ""              # "permanent · full time", "contract" …
    requirements: list[str] = field(default_factory=list)
    snippet_only: bool = False       # Adzuna returns only the start of the ad; paste the full ad to draft

    @property
    def key(self) -> str:
        return f"job:{self.source}:{self.id}"

    def age_days(self, now: datetime) -> float:
        return max((now - self.created_at).total_seconds() / 86400, 0.0)

    def salary(self) -> str:
        if self.salary_text:
            return self.salary_text
        lo, hi = self.salary_min, self.salary_max
        if not lo and not hi:
            return ""
        text = f"${lo:,.0f}" if lo == hi or not hi else (f"${lo:,.0f}–${hi:,.0f}" if lo else f"up to ${hi:,.0f}")
        return text + (" (est.)" if self.salary_predicted else "")

    def to_json(self) -> str:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return json.dumps(d)

    @classmethod
    def from_json(cls, data: str) -> Job:
        d = json.loads(data)
        d["created_at"] = datetime.fromisoformat(d["created_at"])
        return cls(**d)

    def as_text(self) -> str:
        """The ad as the ranking/drafting models see it."""
        lines = [f"Title: {self.title}", f"Company: {self.company or 'unknown'}"]
        for label, value in (("Location", self.location), ("Salary", self.salary()), ("Work type", self.work_type)):
            if value:
                lines.append(f"{label}: {value}")
        if self.requirements:
            lines.append("Key requirements:\n" + "\n".join(f"- {r}" for r in self.requirements))
        note = " (only the start of the ad)" if self.snippet_only else ""
        lines.append(f"\nAd text{note}:\n{self.description}")
        return "\n".join(lines)


@dataclass
class RankedJob:
    job: Job
    fit: int        # 0-10: how well your CV matches
    angle: str = ""  # what to lead with
    gaps: list[str] = field(default_factory=list)  # requirements your CV doesn't show
