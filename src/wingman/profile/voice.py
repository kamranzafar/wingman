"""Your interest + voice profile: built once by Claude from your writing, then read on every run."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from wingman.core.llm import LLM
from wingman.core.models import ActivityItem, Document
from wingman.errors import NotReadyError


class Topic(BaseModel):
    name: str
    tier: Literal["expert", "contributor", "interested"]
    evidence: str


class Voice(BaseModel):
    tone: str
    typical_length: str
    habits: list[str]
    avoid: list[str]
    example_sentences: list[str]


class BlogPost(BaseModel):
    title: str
    url: str
    topics: list[str]
    one_line_summary: str


class Profile(BaseModel):
    summary: str
    topics: list[Topic]
    opinions: list[str]
    voice: Voice
    blog_posts: list[BlogPost]

    def brief(self) -> str:
        """Compact text form used in ranking/drafting system prompts."""
        v = self.voice
        lines = [f"Summary: {self.summary}", "", "Topics:"]
        lines += [f"- {t.name} ({t.tier})" for t in self.topics]
        lines += ["", "Opinions:"] + [f"- {o}" for o in self.opinions]
        lines += ["", f"Voice: {v.tone}. Typical length: {v.typical_length}.",
                  "Habits: " + "; ".join(v.habits), "Never: " + "; ".join(v.avoid),
                  "Example sentences:"] + [f'  "{s}"' for s in v.example_sentences]
        lines += ["", "Blog posts:"] + [f"- {b.title} <{b.url}>: {b.one_line_summary}" for b in self.blog_posts]
        return "\n".join(lines)


SYSTEM = """You build a profile of a person from their own writing, so that a copilot tool can \
(1) find online discussions they could genuinely contribute to and (2) help them draft replies \
in their own voice.

Be accurate and conservative:
- Topic tiers: "expert" only where the writing shows hands-on depth; "contributor" where they \
show solid working knowledge; "interested" for topics they mention but don't go deep on.
- Evidence: cite which posts/comments support each topic.
- Opinions: specific positions they actually hold in the writing, not generic ones.
- Voice: describe how they actually write (sentence length, formality, humor, jargon, \
formatting habits) and list things their writing never does. Example sentences must be \
quoted or closely paraphrased from their writing (blog posts and comments, not CV/profile \
pages, which are written in a formal register).
- Background pages (homepage, LinkedIn export, CV, GitHub repos) tell you their roles, \
experience and current focus: use them for topics and tiers, weighting recent work over old \
blog posts. Projects they built and maintain (GitHub repos, product sites) are strong evidence \
of hands-on engineering depth.
- blog_posts: one entry per blog page provided, using its real URL. Don't list background pages."""

MAX_COMMENTS = 40


def build_profile(llm: LLM, blog: list[Document], background: list[Document],
                  comments: list[ActivityItem], notes: str) -> Profile:
    parts = []
    if background:
        parts.append("## Background (homepage, LinkedIn export, CV, GitHub, project sites)\n")
        for b in background:
            url = f"URL: {b.url}\n" if b.url else ""
            parts.append(f"### {b.title}\n{url}\n{b.text}\n")
    parts.append("## Blog pages\n")
    parts += [f"### {p.title}\nURL: {p.url}\n\n{p.text}\n" for p in blog]
    if comments:
        top = sorted(comments, key=lambda c: c.score, reverse=True)[:MAX_COMMENTS]
        parts.append("## Their past comments (highest scoring first)\n")
        parts += [f"- [{c.source}, score {c.score}] {c.body[:600]}" for c in top]
    if notes:
        parts.append(f"## Notes from the person about themselves\n{notes}")
    return llm.parse(system=SYSTEM, prompt="\n".join(parts), schema=Profile, effort="medium",
                     max_tokens=32000, task="profile")


class ProfileStore:
    """profile.json in the data directory; edit it freely, it's re-read on every run."""

    def __init__(self, data_dir: Path):
        self.path = data_dir / "profile.json"

    def save(self, profile: Profile) -> Path:
        self.path.write_text(profile.model_dump_json(indent=2))
        return self.path

    def load(self) -> Profile:
        if not self.path.exists():
            raise NotReadyError("No profile yet. Run `wingman profile build` first.")
        return Profile.model_validate_json(self.path.read_text())
