"""Drafts reply options in the user's voice. Drafts are suggestions; nothing posts from here."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from wingman.core import privacy
from wingman.core.llm import LLM
from wingman.core.models import Constraints, RankedThread, Thread
from wingman.core.retrieval import Index

SYSTEM_TEMPLATE = """You are a ghost-writing assistant for one specific person. You draft replies \
that THEY will review, edit and post themselves under their own name. Write as them, in first \
person, matching their voice exactly.

Rules:
- Add something the existing top comments don't already say. If you can't, set should_reply \
to false and explain why.
- Never invent personal experiences, credentials, numbers, or anecdotes that aren't supported by \
their profile. Where a personal story would help, leave a placeholder like \
[your experience with X here] for them to fill in.
- Privacy: never name their current or past employers, clients, colleagues or internal \
systems, even though the profile mentions them. Describe work generically at most \
("at a large bank", "on a document platform I worked on"). Their own open-source projects \
and blog are fine to mention.
- Respect the community's rules. rule_concerns lists only real risks: a rule this thread or a \
draft could actually break (self-promotion limits, no-AI-content rules, required formats). \
Leave it empty when there are none; don't list rules the drafts already follow.
- Blog links: {link_policy} Only link posts listed in the profile, by their exact URL.
- Length: these are comments, not articles. Aim for about {target_chars} characters; never more \
than {max_chars} (hard limit, counted by the platform, including any link). Shorter is better when \
it says everything needed. {formatting} {platform_notes}
- Give exactly {num_drafts} drafts with clearly different angles (e.g. a short direct answer \
and a fuller explanation, or a contrasting view). Plain, human, specific. No flattery of \
the poster, no "Great question!", no summary sign-offs.
- Each draft is a complete reply that will be posted on its own; the reader never sees the \
other drafts. Never refer to another draft ("as above", "like I said", "see the first option").
- When excerpts from their own writing are provided, use them for specifics: their actual \
patterns, code, config, failure stories and arguments, so the reply sounds like someone who \
has built this. Paraphrase rather than paste whole paragraphs.
- Don't present generalisations as fact ("this usually clears most of them") unless the \
profile or excerpts back them; say it as their opinion or leave it out.
- claims_to_verify: only the specific, checkable facts in the drafts (product features, APIs, \
versions, numbers) that the person should confirm. Skip opinions and restated advice.

PROFILE
{profile}
{learnings}"""

LINK_POLICIES = {
    "never": "never link the blog.",
    "when_directly_relevant": "link a blog post at most once, only if it directly answers the "
                              "question and the community allows it; the reply must stand on its own without the link.",
}
FORMATTING = {
    True: ("Markdown is rendered: put code, config and commands in fenced code blocks (```java ... ```) "
           "and identifiers in `backticks`, so they display correctly."),
    False: "Markdown is NOT rendered: plain text only, with code on its own indented lines.",
}
PLACEHOLDER_MARKER = "[your "  # drafts use "[your … here]" where only the person can fill in
TARGET_SHARE = 0.85  # ask for ~85% of the limit: models can't count characters, so leave headroom

SHORTEN_SYSTEM = """You tighten drafts of social media replies/posts to fit a hard character limit. \
Keep the author's voice, their main point, every URL exactly as written, and code identifiers. Cut \
filler, hedges, repetition and secondary points first. Never add new claims or change the meaning. \
Return one shortened text per draft, in the same order."""


def target_length(max_chars: int) -> int:
    return int(max_chars * TARGET_SHARE)


class Shortened(BaseModel):
    texts: list[str]


URL = re.compile(r"https?://\S+")
CHARS_PER_WORD = 6  # English prose incl. the space; models follow word budgets far better than character counts
MAX_SHORTEN_PASSES = 2


def prose_budget(limit: int, text: str) -> int:
    """Characters left for the words once any links (which count in full) are taken out."""
    links = URL.findall(text)
    return limit - sum(len(u) + 1 for u in links)


def _shorten_brief(text: str, limit: int, measure: Callable[[str], int]) -> str:
    """Concrete numbers for one over-long draft: models can't count characters, so spell out the cut."""
    budget = prose_budget(limit, text)
    target = target_length(budget)
    prose = measure(URL.sub("", text).strip())
    cut = prose - target
    links = ", ".join(URL.findall(text))
    link_note = f" The link ({links}) must stay exactly as is and isn't part of that budget." if links else ""
    return (f"Currently {prose} characters of words. Rewrite the words in at most {target} characters "
            f"(about {target // CHARS_PER_WORD} words), i.e. cut about {cut} characters "
            f"(~{max(cut // CHARS_PER_WORD, 1)} words).{link_note}")


def fit_to_limit(llm: LLM, drafts: DraftSet, limit: int, measure: Callable[[str], int]) -> DraftSet:
    """Tighten only the drafts over the limit, up to two passes. A rewrite is kept only if it's shorter."""
    for _ in range(MAX_SHORTEN_PASSES):
        over = [i for i, d in enumerate(drafts.drafts) if measure(d.text) > limit]
        if not over:
            break
        listing = "\n\n".join(f"[{n}] {_shorten_brief(drafts.drafts[i].text, limit, measure)}\n"
                               f"{drafts.drafts[i].text}" for n, i in enumerate(over, 1))
        prompt = f"Hard limit: {limit} characters per draft in total, links included.\n\n{listing}"
        result = llm.parse(system=SHORTEN_SYSTEM, prompt=prompt, schema=Shortened, task="draft", effort="low")
        for i, text in zip(over, result.texts, strict=False):
            text = text.strip()
            if text and measure(text) < measure(drafts.drafts[i].text):
                drafts.drafts[i].text = text
    return drafts


class Draft(BaseModel):
    angle: str
    text: str


class DraftSet(BaseModel):
    should_reply: bool
    reason: str
    rule_concerns: list[str]
    drafts: list[Draft]
    claims_to_verify: list[str]

    @classmethod
    def empty(cls) -> DraftSet:
        return cls(should_reply=True, reason="", rule_concerns=[], drafts=[], claims_to_verify=[])


@dataclass
class DraftResult:
    drafts: DraftSet
    sources: list[str] = field(default_factory=list)  # titles of your writing the drafts drew on
    model: str = ""  # which model wrote them (for quality tracking)


def learnings_path(data_dir: Path) -> Path:
    return data_dir / "learnings.md"


@dataclass(frozen=True)
class DraftingOptions:
    link_policy: str = "when_directly_relevant"
    num_drafts: int = 2
    excerpts: int = 4
    private_terms: tuple[str, ...] = ()
    max_chars: int = 1500
    # Cache the (large, identical) system prompt only when several drafts follow each other
    # within minutes, e.g. a review session; one-off drafts would pay the cache-write premium for nothing.
    cache_prompt: bool = False


class Drafter:
    def __init__(self, llm: LLM, profile_brief: str, constraints: Constraints, options: DraftingOptions,
                 index: Index | None = None, learnings: str = ""):
        self.llm = llm
        self.index = index
        self.options = options
        self.max_chars = min(options.max_chars, constraints.max_length)
        self.measure = constraints.length
        # The drafting model never sees private names: they're swapped for a neutral placeholder.
        self.system = SYSTEM_TEMPLATE.format(
            num_drafts=options.num_drafts,
            link_policy=LINK_POLICIES[options.link_policy],
            max_chars=self.max_chars,
            target_chars=target_length(self.max_chars),
            formatting=FORMATTING[constraints.markdown],
            platform_notes=constraints.notes,
            profile=self._redact(profile_brief),
            learnings=f"\nWHAT HAS WORKED FOR THEM BEFORE\n{learnings}" if learnings else "",
        )

    def _redact(self, text: str) -> str:
        return privacy.redact(text, list(self.options.private_terms))

    def draft(self, thread: Thread, ranking: RankedThread | None = None, instruction: str = "") -> DraftResult:
        parts = [
            f"Community: {thread.source}",
            "Community rules:\n" + ("\n".join(f"- {r}" for r in thread.source_rules) or "- (none listed)"),
            f"\nThread title: {thread.title}\nPosted by: {thread.author}\n\n{thread.body or '(no body)'}",
            "\nTop existing replies:",
        ]
        parts += [f"- ({c.score} pts) {c.body}" for c in thread.top_comments] or ["- (none yet)"]
        sources: list[str] = []
        if self.index and self.options.excerpts:
            hits = self.index.search(f"{thread.title}\n{thread.body[:1500]}", k=self.options.excerpts)
            if hits:
                parts.append("\nRelevant excerpts from their own writing "
                             "(for specifics; links only per the blog rule):")
                parts += [f"--- {h.source} ({h.kind}{', ' + h.url if h.url else ''})\n{self._redact(h.text)}"
                          for h in hits]
                sources = list(dict.fromkeys(h.source for h in hits))
        if ranking and ranking.angle:
            parts.append(f"\nSuggested angle from triage: {ranking.angle}")
        if instruction:
            parts.append(f"\nThe person asked for this change: {instruction}")
        drafts = self.llm.parse(system=self.system, prompt="\n".join(parts), schema=DraftSet, task="draft",
                                cache=self.options.cache_prompt)
        drafts = fit_to_limit(self.llm, drafts, self.max_chars, self.measure)
        return DraftResult(drafts=drafts, sources=sources, model=self.llm.model_for("draft"))
