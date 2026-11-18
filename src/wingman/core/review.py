"""Interactive terminal review. The only place a reply can be posted from."""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import typer
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.text import Text

from wingman.config import PacingSettings
from wingman.core import clipboard, privacy, quality
from wingman.core.drafter import PLACEHOLDER_MARKER, Drafter, DraftResult, DraftSet
from wingman.core.models import LinkCard, RankedThread, Reply, Thread, now_utc
from wingman.core.pacing import next_allowed
from wingman.core.store import Store
from wingman.errors import LLMError
from wingman.platforms.base import Platform
from wingman.ui import console

HELP = ("[bold]1-4[/] pick draft  [bold]e[/] edit  [bold]r[/] regenerate  [bold]w[/] write your own  "
        "[bold]o[/] open  [bold]p[/] post  [bold]s[/] skip  [bold]n[/] next (keep for later)  [bold]b[/] back  "
        "[bold]q[/] quit")
HELP_SINGLE = ("[bold]1-4[/] pick draft  [bold]e[/] edit  [bold]r[/] regenerate  [bold]w[/] write your own  "
               "[bold]o[/] open  [bold]p[/] post  [bold]q[/] done")


class Outcome(Enum):
    DONE = "done"  # posted, queued or skipped
    NEXT = "next"  # leave for a later review
    BACK = "back"  # return to the previous thread
    QUIT = "quit"


@dataclass
class Selection:
    """What the user is about to post: one of the drafts, or their own edited/written text."""
    result: DraftResult = field(default_factory=lambda: DraftResult(DraftSet.empty()))
    index: int = 0
    own_text: str | None = None
    regenerations: int = 0

    @property
    def text(self) -> str:
        if self.own_text is not None:
            return self.own_text
        drafts = self.result.drafts.drafts
        return drafts[self.index].text if drafts else ""


class ReviewSession:
    def __init__(self, platform: Platform, store: Store, drafter: Drafter | None, pacing: PacingSettings,
                 private_terms: list[str], max_chars: int | None = None, ask_rating: bool = True,
                 new_post: bool = False, link: LinkCard | None = None):
        self.ask_rating = ask_rating
        # new_post: the "thread" is just your topic; `p` publishes a standalone post (with `link` as its card).
        self.new_post = new_post
        self.link = link
        self.platform = platform
        self.store = store
        self.drafter = drafter
        self.pacing = pacing
        self.private_terms = private_terms
        # Your comment-length limit, never above what the platform itself accepts.
        self.max_chars = min(max_chars or platform.constraints.max_length, platform.constraints.max_length)
        # Per-thread state for this session, so going back (b) shows the same drafts and edits
        # without paying for new drafts, and can't post or rate the same thread twice.
        self._selections: dict[str, Selection] = {}
        self._fetched: set[str] = set()
        self._posted: set[str] = set()
        self._rated: set[str] = set()

    # ------------------------------------------------------------ entry points
    def run(self, items: list[RankedThread]) -> None:
        """Review ranked threads from `wingman scan`, one after another."""
        if not items:
            console.print("Nothing in the queue. Run `wingman scan` first (or lower --min-score).")
            return
        i = 0
        while i < len(items):
            ranked = items[i]
            console.rule(f"[bold]{i + 1}/{len(items)}")
            if ranked.thread.key not in self._fetched:
                with console.status("Fetching thread context…"):
                    try:
                        ranked.thread = self.platform.fetch_context(ranked.thread)
                    except Exception as e:  # noqa: BLE001 - deleted threads, blocked feeds etc.
                        console.print(f"[yellow]Couldn't load existing replies ({e}). Drafting without them; "
                                      "open the thread with [bold]o[/] to check what's been said.[/]")
                self._fetched.add(ranked.thread.key)
            outcome = self.review(ranked.thread, ranked, first=i == 0)
            if outcome is Outcome.QUIT:
                return
            i = i - 1 if outcome is Outcome.BACK else i + 1
        console.print("[dim]End of the queue.[/]")

    def review(self, thread: Thread, ranking: RankedThread | None = None, single: bool = False,
               first: bool = False) -> Outcome:
        """Draft, pick/edit and post for one thread. Revisiting a thread (b) reuses its drafts."""
        self._show_thread(thread, ranking)
        sel = self._selections.get(thread.key)
        if sel is None:
            sel = Selection()
            if self.drafter:
                sel.result = self._draft(thread, ranking) or sel.result
            else:
                console.print("[yellow]AI drafts are disabled for this platform. "
                              "Write your own reply with [bold]w[/].[/]")
            self._selections[thread.key] = sel
        if thread.key in self._posted:
            console.print("[green]You've already replied to this thread in this session.[/]")

        while True:
            self._show_drafts(sel)
            console.print(HELP_SINGLE if single else HELP)
            choice = Prompt.ask(">", default="q" if single else "n").strip().lower()
            drafts = sel.result.drafts.drafts
            if choice.isdigit() and 1 <= int(choice) <= len(drafts):
                sel.index, sel.own_text = int(choice) - 1, None
            elif choice == "e" and sel.text:
                edited = typer.edit(sel.text, extension=".md")
                if edited is not None:
                    sel.own_text = edited.strip()
            elif choice == "w":
                written = typer.edit("", extension=".md")
                if written and written.strip():
                    sel.own_text = written.strip()
            elif choice == "r" and self.drafter:
                instruction = Prompt.ask("What should change? (enter for a fresh attempt)", default="")
                result = self._draft(thread, ranking, instruction)
                if result:
                    sel = self._selections[thread.key] = Selection(result, regenerations=sel.regenerations + 1)
            elif choice == "o":
                webbrowser.open(thread.url)
            elif choice == "p":
                if not sel.text:
                    console.print("[red]Nothing to post yet.[/]")
                elif posted := self._post(thread, sel.text):
                    self._rate(thread, sel, posted)
                    return Outcome.DONE
            elif choice == "s" and not single:
                self.store.set_status(thread.key, "skipped")
                self._rate(thread, sel, "skipped")
                return Outcome.DONE
            elif choice == "n" and not single:
                return Outcome.NEXT  # you'll see it again, so no rating yet
            elif choice == "b" and not single:
                if first:
                    console.print("[yellow]This is the first thread.[/]")
                else:
                    return Outcome.BACK
            elif choice == "q":
                self._rate(thread, sel, "abandoned")
                return Outcome.QUIT

    # ------------------------------------------------------------ drafting
    def _draft(self, thread: Thread, ranking: RankedThread | None, instruction: str = "") -> DraftResult | None:
        try:
            with console.status("Redrafting…" if instruction else "Drafting replies…"):
                result = self.drafter.draft(thread, ranking, instruction)
        except LLMError as e:
            console.print(f"[red]{e}[/]")
            return None
        if result.sources:
            console.print("[dim]  drew on: " + " · ".join(s[:50] for s in result.sources) + "[/]")
        return result

    # ------------------------------------------------------------ posting
    def _blocked(self, text: str) -> str | None:
        """Why this text can't be posted, or None."""
        hits = privacy.find(text, self.private_terms)
        if hits:
            return f"The reply mentions {', '.join(hits)} (in your private_terms). Edit it first."
        length = self.platform.constraints.length(text)
        if length > self.max_chars:
            whose = (f"{self.platform.name.title()}'s limit" if self.max_chars == self.platform.constraints.max_length
                     else "your max_draft_chars")
            return (f"Too long: {length:,} characters; {whose} is {self.max_chars:,}. "
                    "Press e to trim it, or r and ask for something like 'under 250 characters'.")
        if PLACEHOLDER_MARKER in text.lower():
            return "The draft still has a [your …] placeholder. Edit it first."
        return None

    def _post(self, thread: Thread, text: str) -> quality.Outcome | None:
        """'posted' or 'queued' when the thread is done, None if not posted."""
        if thread.key in self._posted:
            console.print("[red]You've already replied to this thread in this session.[/]")
            return None
        problem = self._blocked(text)
        if problem:
            console.print(f"[red]{problem}[/]")
            return None
        console.print(Panel(text, title=f"About to post to {thread.source}", border_style="yellow"))
        wait_until = next_allowed(self.store, self.platform.name, thread.source, self.pacing)
        post = self._post_manual if self.platform.posting == "manual" else self._post_api
        outcome = post(thread, text, wait_until)
        if outcome:
            self._posted.add(thread.key)
        return outcome

    def _post_api(self, thread: Thread, text: str, wait_until: datetime | None) -> quality.Outcome | None:
        p, store = self.platform, self.store
        if self.new_post and self.link and self.link.uri not in text and \
                not Confirm.ask("This draft doesn't include the article link. Post anyway?", default=False):
            return None
        if wait_until and self.new_post:  # nothing to queue against; your own posts are your call
            local = wait_until.astimezone().strftime("%H:%M")
            if not Confirm.ask(f"Pacing: you've hit your posting limit until {local}. Post anyway?", default=False):
                return None
        elif wait_until:
            local = wait_until.astimezone().strftime("%H:%M")
            if not Confirm.ask(f"Pacing limit reached. Queue it for {local} (post later with `wingman flush`)?"):
                return None
            store.record_post(p.name, thread.source, thread.key, text, "queued", due_at=wait_until)
            store.set_status(thread.key, "queued")
            console.print("[green]Queued.[/]")
            return "queued"
        if not Confirm.ask("Post now?", default=False):
            return None
        try:
            if self.new_post:
                url = p.publish(text, self.link)
            else:
                url = p.submit(Reply(p.name, thread.id, thread.source, text))
        except Exception as e:  # noqa: BLE001 - show any platform error and keep going
            store.record_post(p.name, thread.source, thread.key, text, "failed", error=str(e))
            console.print(f"[red]Post failed:[/] {e}")
            return None
        store.record_post(p.name, thread.source, thread.key, text, "posted", url=url)
        store.set_status(thread.key, "posted")
        console.print(f"[green]Posted:[/] {url}")
        return "posted"

    def _post_manual(self, thread: Thread, text: str, wait_until: datetime | None) -> quality.Outcome | None:
        """Copy + open the thread; you paste and submit. 'posted' if you did."""
        if wait_until:
            local = wait_until.astimezone().strftime("%H:%M")
            console.print(f"[yellow]Pacing: you've hit your posting limit until {local}.[/]")
            if not Confirm.ask("Post anyway?", default=False):
                return None
        if clipboard.copy(text):
            console.print("[green]Draft copied to your clipboard.[/] Opening the thread…")
        else:
            console.print("[yellow]No clipboard tool found; copy the draft above by hand.[/]")
        webbrowser.open(thread.url)
        console.print("Check the community's rules, paste the reply, and submit it.")
        answer = Prompt.ask("Paste your comment's link (or press enter if posted, 'x' if you didn't post)",
                            default="").strip()
        if answer.lower() == "x":
            return None
        self.store.record_post(self.platform.name, thread.source, thread.key, text, "posted", url=answer or None)
        self.store.set_status(thread.key, "posted")
        console.print("[green]Recorded as posted.[/]")
        return "posted"

    # ------------------------------------------------------------ rating
    def _rate(self, thread: Thread, sel: Selection, outcome: quality.Outcome) -> None:
        """One keypress after each thread with drafts, feeding `wingman quality`. Enter skips."""
        drafts = sel.result.drafts.drafts
        if not self.ask_rating or not drafts or thread.key in self._rated:
            return
        sim = quality.similarity(drafts[sel.index].text, sel.text) if outcome in ("posted", "queued") else None
        default = str(quality.suggested_rating(sim)) if sim is not None else ""
        scale = "  ".join(f"[bold]{n}[/] {label}" for n, label in quality.SCALE.items())
        console.print(f"[dim]Rate these drafts:[/] {scale}")
        answer = Prompt.ask("rating (enter " + (f"= {default}" if default else "to skip") + ")",
                            default=default, show_default=False).strip()
        if answer not in {"1", "2", "3", "4", "5"}:
            return
        self._rated.add(thread.key)
        rating, issues = int(answer), []
        if rating <= quality.LOW_RATING:
            options = "  ".join(f"[bold]{k}[/] {v}" for k, v in quality.ISSUES.items())
            issues = quality.parse_issues(Prompt.ask(f"What was off? {options} (letters, enter to skip)",
                                                     default="", show_default=False))
        self.store.add_rating(quality.Rating(
            platform=self.platform.name, thread_key=thread.key, source=thread.source,
            model=sel.result.model or "unknown", rating=rating, outcome=outcome, similarity=sim,
            regenerations=sel.regenerations, issues=issues))

    # ------------------------------------------------------------ display
    def _show_thread(self, t: Thread, ranking: RankedThread | None) -> None:
        if self.new_post:
            link = f"\n\n[dim]Sharing: {self.link.title} · {self.link.uri}[/]" if self.link else ""
            notes = f"\n\n{t.body}" if t.body and t.body != t.title else ""
            console.print(Panel(f"[bold]{t.title}[/]{notes}{link}", title=f"New {self.platform.name} post",
                                title_align="left"))
            return
        bits = []
        if ranking:
            bits.append(f"{t.age_hours(now_utc()):.1f}h old")
        if self.platform.has_scores:
            bits.append(f"{t.score} pts")
        if t.num_comments:
            bits.append(f"{t.num_comments} comments")
        if ranking:
            bits += [f"relevance {ranking.relevance:.0f}/10", f"opportunity {ranking.opportunity:.2f}"]
        head = Text.assemble((t.source, "bold cyan"), "".join(f"  ·  {b}" for b in bits))
        body = (t.body[:1200] + ("…" if len(t.body) > 1200 else "")) or "(no body)"
        console.print(Panel(f"[bold]{t.title}[/]\n\n{body}\n\n[dim]{t.url}[/]", title=head, title_align="left"))
        if ranking and ranking.angle:
            console.print(f"[magenta]Angle:[/] {ranking.angle}")
        for c in t.top_comments[:3]:
            score = f"({c.score}) " if self.platform.has_scores else ""
            console.print(f"[dim]  ↳ {score}{c.body[:200].replace(chr(10), ' ')}[/]")

    def _length_label(self, text: str) -> str:
        n = self.platform.constraints.length(text)
        return f"[red]{n}/{self.max_chars} too long[/]" if n > self.max_chars else f"[dim]{n}/{self.max_chars}[/]"

    def _show_drafts(self, sel: Selection) -> None:
        ds = sel.result.drafts
        if not ds.should_reply:
            console.print(f"[yellow]Wingman suggests not posting this:[/] {ds.reason}")
        for i, d in enumerate(ds.drafts):
            style = "green" if sel.own_text is None and i == sel.index else "white"
            console.print(Panel(d.text, title=f"[{i + 1}] {d.angle}", title_align="left", border_style=style,
                                subtitle=self._length_label(d.text), subtitle_align="right"))
        if sel.own_text is not None:
            console.print(Panel(sel.own_text, title="[your edit]", title_align="left", border_style="green",
                                subtitle=self._length_label(sel.own_text), subtitle_align="right"))
        if ds.rule_concerns:
            console.print("[red]Rule concerns:[/] " + " | ".join(ds.rule_concerns))
        if ds.claims_to_verify:
            console.print("[yellow]Verify before posting:[/] " + " | ".join(ds.claims_to_verify))
