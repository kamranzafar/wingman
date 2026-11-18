"""Drafts new standalone posts (not replies) in the user's voice: from a topic, or sharing a blog post."""

from __future__ import annotations

import hashlib

from wingman.core import privacy
from wingman.core.drafter import (
    CHARS_PER_WORD,
    FORMATTING,
    DraftingOptions,
    DraftResult,
    DraftSet,
    fit_to_limit,
    prose_budget,
    target_length,
)
from wingman.core.llm import LLM
from wingman.core.models import Constraints, Document, LinkCard, RankedThread, Thread, now_utc
from wingman.core.retrieval import Index

SYSTEM_TEMPLATE = """You are a ghost-writing assistant for one specific person. You draft NEW \
standalone posts that THEY will review, edit and publish under their own name. Write as them, in \
first person, matching their voice exactly.

Rules:
- A post must stand on its own for someone scrolling past: lead with the specific, useful or \
surprising point, not with throat-clearing ("Some thoughts on…", "Hot take:", "Thread 🧵").
- Never invent personal experiences, credentials, numbers, or anecdotes that aren't supported by \
their profile or excerpts. Where a personal story would help, leave a placeholder like \
[your experience with X here] for them to fill in.
- Privacy: never name their current or past employers, clients, colleagues or internal \
systems, even though the profile mentions them. Describe work generically at most. Their own \
open-source projects and blog are fine to mention.
- Length: aim for about {target_chars} characters; never more than {max_chars} (hard limit), \
INCLUDING any link, which counts in full. {formatting} {platform_notes}
- Give exactly {num_drafts} drafts with clearly different angles (e.g. the key lesson stated \
plainly, a concrete before/after, or a question that invites people who've hit the same problem). \
Each draft is published on its own; never refer to another draft.
- At most one hashtag, and only if it's how people on the platform actually find the topic.
- Don't present generalisations as fact unless the profile or excerpts back them.
- claims_to_verify: only specific, checkable facts in the drafts (product features, APIs, versions, \
numbers). Skip opinions.
- should_reply means "worth publishing": set it false (with the reason) only if the topic gives \
them nothing distinctive to say.

PROFILE
{profile}"""

SHARE_RULE = ("This post shares their article. Include this exact URL once in every draft: {url}\n"
              "The link uses {link_chars} of the {max_chars} characters, so the words must fit in about "
              "{words_chars} characters (roughly {words} words). Say what the reader gets from the article "
              "(the specific problem or finding), not just that it exists.")


class Composer:
    """Same contract as Drafter (`draft(thread, ranking, instruction)`), so review works unchanged.

    The 'thread' it receives is the synthetic one built by `compose_target`.
    """

    def __init__(self, llm: LLM, profile_brief: str, constraints: Constraints, options: DraftingOptions,
                 index: Index | None = None, article: Document | None = None):
        self.llm = llm
        self.index = index
        self.options = options
        self.article = article
        self.max_chars = min(options.max_chars, constraints.max_length)
        self.measure = constraints.length
        self.system = SYSTEM_TEMPLATE.format(
            num_drafts=options.num_drafts, max_chars=self.max_chars, target_chars=target_length(self.max_chars),
            formatting=FORMATTING[constraints.markdown],
            platform_notes=constraints.notes, profile=self._redact(profile_brief))

    def _redact(self, text: str) -> str:
        return privacy.redact(text, list(self.options.private_terms))

    def draft(self, thread: Thread, ranking: RankedThread | None = None, instruction: str = "") -> DraftResult:
        topic = thread.title
        parts = [f"Write a new post about: {topic}"]
        if thread.body and thread.body != topic:
            parts.append(f"Their notes: {thread.body}")
        if self.article:
            budget = target_length(prose_budget(self.max_chars, self.article.url))
            parts.append("\n" + SHARE_RULE.format(url=self.article.url, link_chars=len(self.article.url) + 1,
                                                  max_chars=self.max_chars, words_chars=budget,
                                                  words=budget // CHARS_PER_WORD))
            parts.append(f"\nThe article ({self.article.title}):\n{self._redact(self.article.text[:6000])}")
        sources: list[str] = []
        if self.index and self.options.excerpts:
            query = f"{topic}\n{self.article.text[:1500] if self.article else thread.body}"
            hits = [h for h in self.index.search(query, k=self.options.excerpts)
                    if not (self.article and h.url == self.article.url)]  # the article itself is already included
            if hits:
                parts.append("\nRelevant excerpts from their other writing (for specifics):")
                parts += [f"--- {h.source} ({h.kind})\n{self._redact(h.text)}" for h in hits]
                sources = list(dict.fromkeys(h.source for h in hits))
        if instruction:
            parts.append(f"\nThe person asked for this change: {instruction}")
        drafts = self.llm.parse(system=self.system, prompt="\n".join(parts), schema=DraftSet, task="draft",
                                cache=self.options.cache_prompt)
        drafts = fit_to_limit(self.llm, drafts, self.max_chars, self.measure)
        return DraftResult(drafts=drafts, sources=sources, model=self.llm.model_for("draft"))


def compose_target(platform: str, topic: str, notes: str = "", url: str = "") -> Thread:
    """The 'thread' a new post is reviewed against: there's no parent, just your topic."""
    digest = hashlib.sha1(f"{topic}|{url}|{now_utc().isoformat()}".encode()).hexdigest()[:12]
    return Thread(platform=platform, id=f"post-{digest}", source="your feed", title=topic, body=notes,
                  url=url, author="you", score=0, num_comments=0, created_at=now_utc())


__all__ = ["Composer", "LinkCard", "compose_target"]
