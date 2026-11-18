"""Draft quality tracking: your 1-5 ratings plus how much you edited a draft before posting."""

from __future__ import annotations

import difflib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from statistics import mean
from typing import Literal

Outcome = Literal["posted", "queued", "skipped", "abandoned"]

SCALE = {
    5: "posted as-is",
    4: "light edit",
    3: "heavy edit",
    2: "rewrote it / used only the idea",
    1: "unusable or wrong",
}
ISSUES = {"f": "facts", "v": "voice", "r": "relevance", "l": "length", "o": "other"}
LOW_RATING = 3  # at or below this, ask what was off


def similarity(draft: str, final: str) -> float:
    """0-1: how much of the draft survived into what you posted (1.0 = unchanged)."""
    return round(difflib.SequenceMatcher(None, draft, final, autojunk=False).ratio(), 3)


def suggested_rating(sim: float) -> int:
    """A default rating from the edit you made, so posting a draft usually needs just Enter."""
    if sim >= 0.97:
        return 5
    if sim >= 0.80:
        return 4
    if sim >= 0.50:
        return 3
    return 2


def parse_issues(answer: str) -> list[str]:
    """'fv', 'f v' or 'facts, voice' -> ['facts', 'voice']; unknown letters are ignored."""
    found: list[str] = []
    for token in answer.lower().replace(",", " ").split():
        named = [name for name in ISSUES.values() if len(token) >= 3 and name.startswith(token)]
        found += named or [ISSUES[ch] for ch in token if ch in ISSUES]  # a word, or a run of letters
    return list(dict.fromkeys(found))


@dataclass
class Rating:
    platform: str
    thread_key: str
    source: str
    model: str
    rating: int
    outcome: Outcome
    similarity: float | None = None  # only when something was posted from a draft
    regenerations: int = 0
    issues: list[str] = field(default_factory=list)
    at: str = ""


@dataclass
class Group:
    name: str
    count: int
    avg_rating: float
    posted_pct: float
    avg_similarity: float | None


@dataclass
class QualitySummary:
    count: int
    avg_rating: float
    distribution: dict[int, int]
    posted_pct: float
    avg_similarity: float | None
    avg_regenerations: float
    by_model: list[Group]
    by_platform: list[Group]
    issues: list[tuple[str, int]]


def _group(name: str, rows: list[Rating]) -> Group:
    sims = [r.similarity for r in rows if r.similarity is not None]
    return Group(name, len(rows), mean(r.rating for r in rows),
                 100 * sum(r.outcome in ("posted", "queued") for r in rows) / len(rows),
                 mean(sims) if sims else None)


def summarize(ratings: list[Rating]) -> QualitySummary | None:
    if not ratings:
        return None
    by_model: dict[str, list[Rating]] = defaultdict(list)
    by_platform: dict[str, list[Rating]] = defaultdict(list)
    for r in ratings:
        by_model[r.model].append(r)
        by_platform[r.platform].append(r)
    overall = _group("all", ratings)
    return QualitySummary(
        count=len(ratings),
        avg_rating=overall.avg_rating,
        distribution={score: sum(r.rating == score for r in ratings) for score in sorted(SCALE, reverse=True)},
        posted_pct=overall.posted_pct,
        avg_similarity=overall.avg_similarity,
        avg_regenerations=mean(r.regenerations for r in ratings),
        by_model=sorted((_group(m, rows) for m, rows in by_model.items()), key=lambda g: -g.count),
        by_platform=sorted((_group(p, rows) for p, rows in by_platform.items()), key=lambda g: -g.count),
        issues=Counter(i for r in ratings for i in r.issues).most_common(),
    )
