"""Interactive terminal review. The only place a reply can be posted from."""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import typer
from rich import box
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from wingman import ui
from wingman.config import PacingSettings
from wingman.core import clipboard, privacy, quality
from wingman.core.drafter import PLACEHOLDER_MARKER, Drafter, DraftResult, DraftSet
from wingman.core.models import LinkCard, RankedThread, Reply, Thread, now_utc
from wingman.core.pacing import next_allowed
from wingman.core.store import Store
from wingman.errors import LLMError
from wingman.platforms.base import Platform
from wingman.ui import console

BODY_PREVIEW = 1200
REPLY_PREVIEW = 3


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


def _ago(hours: float) -> str:
    if hours < 1:
        return f"{max(int(hours * 60), 1)}m ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


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
        self._carry: list[str] = []  # outcome of the previous thread, shown at the top of the next screen
        self._single = False  # reviewing one thread (draft/post) rather than a queue

    # ------------------------------------------------------------ entry points
    def run(self, items: list[RankedThread]) -> None:
        """Review ranked threads from `wingman scan`, one after another."""
        if not items:
            ui.info("Nothing to review. Run [accent]wingman scan[/] first, or lower [accent]--min-score[/].")
            return
        i = 0
        while i < len(items):
            ranked = items[i]
            self._new_screen(f"Review · {self.platform.name}", f"{i + 1} of {len(items)}")
            if ranked.thread.key not in self._fetched:
                with console.status("Loading the conversation…"):
                    try:
                        ranked.thread = self.platform.fetch_context(ranked.thread)
                    except Exception as e:  # noqa: BLE001 - deleted threads, blocked feeds etc.
                        ui.warn(f"Couldn't load existing replies ({e}). Press [accent]o[/] to read them yourself.")
                self._fetched.add(ranked.thread.key)
            outcome = self.review(ranked.thread, ranked, first=i == 0)
            if outcome is Outcome.QUIT:
                return
            i = i - 1 if outcome is Outcome.BACK else i + 1
        self._flush_carry()
        ui.ok("End of the queue.")

    def review(self, thread: Thread, ranking: RankedThread | None = None, single: bool = False,
               first: bool = False) -> Outcome:
        """Draft, pick/edit and post for one thread. Revisiting a thread (b) reuses its drafts."""
        self._single = single
        if single:
            self._new_screen("New post" if self.new_post else f"Reply · {self.platform.name}", clear=False)
        self._show_thread(thread, ranking)
        sel = self._selections.get(thread.key)
        if sel is None:
            sel = Selection()
            if self.drafter:
                sel.result = self._draft(thread, ranking) or sel.result
            else:
                ui.warn("AI drafts are off for this platform. Press [accent]w[/] to write your own.")
            self._selections[thread.key] = sel
        elif sel.result.sources:
            self._show_sources(sel.result)
        if thread.key in self._posted:
            ui.ok("You've already replied to this thread in this session.")

        while True:
            self._show_drafts(sel)
            choice = ui.key("[accent]›[/]", self._valid_keys(sel, single))
            if choice.isdigit():
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
                instruction = ui.ask("What should change? [muted](enter for a fresh attempt)[/]")
                result = self._draft(thread, ranking, instruction)
                if result:
                    sel = self._selections[thread.key] = Selection(result, regenerations=sel.regenerations + 1)
            elif choice == "o":
                webbrowser.open(thread.url)
                ui.muted("Opened in your browser.")
            elif choice == "p":
                if not sel.text:
                    ui.error("Nothing to post yet: pick a draft or press [accent]w[/] to write one.")
                elif posted := self._post(thread, sel.text):
                    self._rate(thread, sel, posted)
                    return Outcome.DONE
            elif choice == "s":
                self.store.set_status(thread.key, "skipped")
                self._rate(thread, sel, "skipped")
                self._carry.append(f"[muted]Skipped[/] {escape(thread.title[:70])}")
                return Outcome.DONE
            elif choice == "n":
                return Outcome.NEXT  # you'll see it again, so no rating yet
            elif choice == "b":
                if first:
                    ui.warn("This is the first thread.")
                else:
                    return Outcome.BACK
            elif choice == "q":
                self._rate(thread, sel, "abandoned")
                return Outcome.QUIT

    def _valid_keys(self, sel: Selection, single: bool) -> set[str]:
        keys = {str(i) for i in range(1, len(sel.result.drafts.drafts) + 1)} | {"e", "w", "o", "p", "q"}
        if self.drafter:
            keys.add("r")
        if not single:
            keys |= {"s", "n", "b"}
        return keys

    # ------------------------------------------------------------ drafting
    def _draft(self, thread: Thread, ranking: RankedThread | None, instruction: str = "") -> DraftResult | None:
        try:
            with console.status("Rewriting…" if instruction else "Drafting…"):
                result = self.drafter.draft(thread, ranking, instruction)
        except LLMError as e:
            ui.error(str(e))
            return None
        self._show_sources(result)
        return result

    @staticmethod
    def _show_sources(result: DraftResult) -> None:
        if result.sources:
            titles = [s.split(" — ")[0].removeprefix("GitHub repo: ") for s in result.sources]
            ui.meta([("Sources", Text(" · ".join(titles), style="muted"))])

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
            ui.error("You've already replied to this thread in this session.")
            return None
        problem = self._blocked(text)
        if problem:
            ui.error(escape(problem))
            return None
        where = "your feed" if self.new_post else thread.source
        console.print(Panel(Text(text), title=Text.assemble(("Ready to post", "warn"), (f" · {where}", "muted")),
                            title_align="left", border_style="warn", box=box.ROUNDED, padding=(0, 1)))
        wait_until = next_allowed(self.store, self.platform.name, thread.source, self.pacing)
        post = self._post_manual if self.platform.posting == "manual" else self._post_api
        outcome = post(thread, text, wait_until)
        if outcome:
            self._posted.add(thread.key)
        return outcome

    def _post_api(self, thread: Thread, text: str, wait_until: datetime | None) -> quality.Outcome | None:
        p, store = self.platform, self.store
        if self.new_post and self.link and self.link.uri not in text and \
                not ui.confirm("This draft doesn't include the article link. Post anyway?"):
            return None
        if wait_until and self.new_post:  # nothing to queue against; your own posts are your call
            local = wait_until.astimezone().strftime("%H:%M")
            if not ui.confirm(f"You've hit your posting pace until {local}. Post anyway?"):
                return None
        elif wait_until:
            local = wait_until.astimezone().strftime("%H:%M")
            if not ui.confirm(f"You've hit your posting pace. Queue it for {local} (`wingman flush`)?", default=True):
                return None
            store.record_post(p.name, thread.source, thread.key, text, "queued", due_at=wait_until)
            store.set_status(thread.key, "queued")
            ui.ok(f"Queued for {local}.")
            self._carry.append(f"[ok]Queued[/] reply to {escape(thread.title[:60])} for {local}")
            return "queued"
        if not ui.confirm("Post now?"):
            return None
        try:
            with console.status("Posting…"):
                if self.new_post:
                    url = p.publish(text, self.link)
                else:
                    url = p.submit(Reply(p.name, thread.id, thread.source, text))
        except Exception as e:  # noqa: BLE001 - show any platform error and keep going
            store.record_post(p.name, thread.source, thread.key, text, "failed", error=str(e))
            ui.error(f"Post failed: {e}")
            return None
        store.record_post(p.name, thread.source, thread.key, text, "posted", url=url)
        store.set_status(thread.key, "posted")
        ui.ok(f"Posted · [link={url}]{url}[/link]")
        self._carry.append(f"[ok]Posted[/] [link={url}]{escape(thread.title[:60])}[/link]")
        return "posted"

    def _post_manual(self, thread: Thread, text: str, wait_until: datetime | None) -> quality.Outcome | None:
        """Copy + open the thread; you paste and submit. 'posted' if you did."""
        if wait_until:
            local = wait_until.astimezone().strftime("%H:%M")
            if not ui.confirm(f"You've hit your posting pace until {local}. Post anyway?"):
                return None
        if clipboard.copy(text):
            ui.ok("Copied to your clipboard. Opening the thread…")
        else:
            ui.warn("No clipboard tool found: copy the text above by hand.")
        webbrowser.open(thread.url)
        ui.info("Check the community's rules, paste the reply and submit it.")
        answer = ui.ask("Your comment's link [muted](enter if posted without it, x if you didn't post)[/]").strip()
        if answer.lower() == "x":
            return None
        self.store.record_post(self.platform.name, thread.source, thread.key, text, "posted", url=answer or None)
        self.store.set_status(thread.key, "posted")
        ui.ok("Recorded as posted.")
        self._carry.append(f"[ok]Posted[/] reply to {escape(thread.title[:60])}")
        return "posted"

    # ------------------------------------------------------------ rating
    def _rate(self, thread: Thread, sel: Selection, outcome: quality.Outcome) -> None:
        """One keypress after each thread with drafts, feeding `wingman quality`. Enter skips."""
        drafts = sel.result.drafts.drafts
        if not self.ask_rating or not drafts or thread.key in self._rated:
            return
        sim = quality.similarity(drafts[sel.index].text, sel.text) if outcome in ("posted", "queued") else None
        default = str(quality.suggested_rating(sim)) if sim is not None else "s"
        console.print()
        console.print(Text.assemble(("Rate the drafts  ", "title"),
                                    ui.keybar([(str(n), label) for n, label in quality.SHORT.items()],
                                              [("s", "skip")])))
        answer = ui.key("[accent]›[/]", "12345s", default=default)
        if answer == "s":
            return
        self._rated.add(thread.key)
        rating, issues = int(answer), []
        if rating <= quality.LOW_RATING:
            options = "  ".join(f"[accent]{k}[/] {v}" for k, v in quality.ISSUES.items())
            issues = quality.parse_issues(ui.ask(f"What was off? {options} [muted](letters, enter to skip)[/]"))
        self.store.add_rating(quality.Rating(
            platform=self.platform.name, thread_key=thread.key, source=thread.source,
            model=sel.result.model or "unknown", rating=rating, outcome=outcome, similarity=sim,
            regenerations=sel.regenerations, issues=issues))
        ui.muted(f"  Rated {rating}/5{(' · ' + ', '.join(issues)) if issues else ''}")

    # ------------------------------------------------------------ display
    def _new_screen(self, title: str, right: str = "", clear: bool = True) -> None:
        """Start a fresh screen for each thread (on a terminal), keeping the last thread's outcome visible."""
        if clear and console.is_terminal:
            console.clear()
        ui.heading(title, right)
        self._flush_carry()

    def _flush_carry(self) -> None:
        for line in self._carry:
            console.print(f"  {line}")
        if self._carry:
            console.print()
        self._carry.clear()

    def _show_thread(self, t: Thread, ranking: RankedThread | None) -> None:
        if self.new_post:
            body = Text(t.title, style="title")
            if t.body and t.body != t.title:
                body.append(f"\n\n{t.body}")
            if self.link:
                body.append("\n\n").append("Sharing  ", style="muted").append(self.link.title, style="accent")
                body.append(f"\n{self.link.uri}", style="muted")
            console.print(Panel(body, title=Text(f"New {self.platform.name} post", style="accent"), title_align="left",
                                border_style="muted", box=box.ROUNDED, padding=(0, 1)))
            return

        header = Text.assemble((t.source, "accent"))
        if t.author:
            header.append(f"  ·  @{t.author}", style="muted")
        header.append(f"  ·  {_ago(t.age_hours(now_utc()))}", style="muted")
        body = Text(t.title, style="title")
        if t.body and t.body != t.title:
            text = t.body[:BODY_PREVIEW] + ("…" if len(t.body) > BODY_PREVIEW else "")
            body.append("\n\n").append(text)
        stats = []
        if self.platform.has_scores:
            stats.append(f"♥ {t.score}")
        stats.append(f"{t.num_comments} repl{'y' if t.num_comments == 1 else 'ies'}")
        body.append("\n\n").append("  ·  ".join(stats), style="muted")
        body.append("    ").append(t.url, style=f"muted link {t.url}")
        subtitle = None
        if ranking:
            subtitle = Text.assemble(("fit ", "muted"), (f"{ranking.relevance:.0f}/10", "accent"),
                                     ("  ·  timing ", "muted"), (f"{ranking.opportunity:.2f}", "accent"))
        console.print(Panel(body, title=header, title_align="left", subtitle=subtitle, subtitle_align="right",
                            border_style="muted", box=box.ROUNDED, padding=(0, 1)))

        rows: list[tuple[str, Text]] = []
        if ranking and ranking.angle:
            rows.append(("Angle", Text(ranking.angle)))
        for n, c in enumerate(t.top_comments[:REPLY_PREVIEW]):
            score = f"♥{c.score} " if self.platform.has_scores else ""
            line = c.body.replace("\n", " ")
            rows.append(("Replies" if n == 0 else "", Text.assemble((score, "muted"), (line[:160], ""))))
        ui.meta(rows)

    def _length_label(self, text: str) -> Text:
        n = self.platform.constraints.length(text)
        style = "err" if n > self.max_chars else "muted"
        return Text(f"{n}/{self.max_chars}" + (" · too long" if n > self.max_chars else ""), style=style)

    def _draft_panel(self, title: str, text: str, selected: bool) -> Panel:
        marker = ("● ", "accent") if selected else ("○ ", "muted")
        return Panel(Text(text), title=Text.assemble(marker, (title, "title" if selected else "muted")),
                     title_align="left", subtitle=self._length_label(text), subtitle_align="right",
                     border_style="accent" if selected else "muted", box=box.ROUNDED, padding=(0, 1))

    def _show_drafts(self, sel: Selection) -> None:
        ds = sel.result.drafts
        console.print()
        if not ds.should_reply and ds.drafts:
            ui.warn(f"Wingman suggests not posting this: {escape(ds.reason)}")
        panels = [self._draft_panel(f"{i + 1}  {d.angle}", d.text, sel.own_text is None and i == sel.index)
                  for i, d in enumerate(ds.drafts)]
        if len(panels) == 2 and console.width >= ui.WIDE:
            column_width = (console.width - 1) // 2
            height = max(len(console.render_lines(p, console.options.update_width(column_width))) for p in panels)
            for p in panels:
                p.height = height
            grid = Table.grid(expand=True, padding=(0, 1))
            grid.add_column(ratio=1)
            grid.add_column(ratio=1)
            grid.add_row(*panels)
            console.print(grid)
        else:
            for panel in panels:
                console.print(panel)
        if sel.own_text is not None:
            console.print(self._draft_panel("Your version", sel.own_text, selected=True))

        ui.meta([*((Text("! Rules", style="warn"), Text(c)) for c in ds.rule_concerns),
                 *((Text("? Check", style="warn"), Text(c)) for c in ds.claims_to_verify)])

        n = len(ds.drafts)
        choose = [(f"1-{n}" if n > 1 else "1", "choose")] if n else []
        compose = [*choose, ("e", "edit"), *([("r", "rewrite")] if self.drafter else []), ("w", "write")]
        console.print()
        if self._single or self.new_post:
            console.print(ui.keybar(compose, [("o", "open")], [("p", "post"), ("q", "done")]))
        else:
            console.print(ui.keybar(compose, [("o", "open")],
                                    [("p", "post"), ("s", "skip"), ("n", "next"), ("b", "back"), ("q", "quit")]))


__all__ = ["Outcome", "ReviewSession", "Selection"]
