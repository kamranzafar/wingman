# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Interactive job review: read the ad, draft a tailored resume and cover letter, edit, export.

Nothing is sent anywhere: export writes files, and you apply on the job site yourself.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich import box
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from wingman import ui
from wingman.core import clipboard
from wingman.core.llm import LLM
from wingman.core.models import now_utc
from wingman.core.store import Store
from wingman.errors import LLMError
from wingman.jobs import export as exporter
from wingman.jobs import paste
from wingman.jobs.models import Job, RankedJob
from wingman.jobs.writer import Application, ApplicationResult, ApplicationWriter, placeholders
from wingman.ui import console

AD_PREVIEW = 1500
MIN_PASTE_CHARS = 200


class Outcome(Enum):
    DONE = "done"
    NEXT = "next"
    BACK = "back"
    QUIT = "quit"


@dataclass
class Drafts:
    result: ApplicationResult
    resume: str
    letter: str
    exported: Path | None = None

    @classmethod
    def of(cls, result: ApplicationResult) -> Drafts:
        return cls(result, result.application.resume_md.strip(), result.application.cover_letter_md.strip())

    @property
    def current(self) -> Application:
        return self.result.application.model_copy(update={"resume_md": self.resume, "cover_letter_md": self.letter})


def export_blockers(resume: str, letter: str) -> list[str]:
    """Why an application can't be exported yet (unfilled [your … here] placeholders)."""
    problems = []
    for name, text in (("resume", resume), ("cover letter", letter)):
        n = placeholders(text)
        if n:
            problems.append(f"the {name} still has {n} [your … here] placeholder{'s' if n > 1 else ''}")
        if not text.strip():
            problems.append(f"the {name} is empty")
    return problems


class JobReview:
    def __init__(self, store: Store, writer: ApplicationWriter, llm: LLM, output_root: Path,
                 open_url: Callable[[str], object] = webbrowser.open):
        self.store, self.writer, self.llm = store, writer, llm
        self.output_root = output_root
        self.open_url = open_url
        self._drafts: dict[str, Drafts] = {}  # per job, so going back (b) doesn't pay for new drafts

    # ------------------------------------------------------------ entry points
    def run(self, items: list[RankedJob]) -> None:
        if not items:
            ui.info("No jobs to review. Run [accent]wingman jobs scan[/] first, or lower [accent]--min-fit[/].")
            return
        i = 0
        while i < len(items):
            console.clear()
            ui.heading("Jobs", f"{i + 1} of {len(items)}")
            outcome = self.review(items[i])
            if outcome is Outcome.QUIT:
                return
            i = max(i - 1, 0) if outcome is Outcome.BACK else i + 1
        ui.ok("End of the list.")

    def review(self, ranked: RankedJob, single: bool = False) -> Outcome:
        job = ranked.job
        self._show_job(ranked)
        if job.key in self._drafts:
            return self._drafts_screen(ranked, self._drafts[job.key], single)
        while True:
            keys = [("d", "draft resume + letter"), ("o", "open ad")]
            nav = [("s", "skip for good")] + ([] if single else [("n", "next"), ("b", "back")]) + [("q", "quit")]
            console.print(ui.keybar(keys, nav))
            choice = ui.key("[accent]›[/]", "dosq" + ("" if single else "nb"))
            if choice == "o":
                self.open_url(job.url)
                ui.muted("Opened in your browser.")
            elif choice == "s":
                self.store.set_job_status(job.key, "skipped")
                ui.muted("Skipped; it won't come up again.")
                return Outcome.DONE
            elif choice == "n":
                return Outcome.NEXT
            elif choice == "b":
                return Outcome.BACK
            elif choice == "q":
                return Outcome.QUIT
            elif choice == "d":
                if job.snippet_only and not self._fill_full_ad(ranked):
                    continue
                drafts = self._draft(ranked)
                if drafts:
                    self._drafts[job.key] = drafts
                    return self._drafts_screen(ranked, drafts, single)

    # ------------------------------------------------------------ steps
    def _fill_full_ad(self, ranked: RankedJob) -> bool:
        """Adzuna gives only the start of an ad: get the full text from the ad page you copy."""
        job = ranked.job
        ui.warn("Adzuna only shares the start of this ad. For a properly tailored application, open it, "
                "copy the whole page (select all, copy), then press [accent]c[/].")
        while True:
            console.print(ui.keybar([("o", "open ad"), ("c", "I've copied it"), ("e", "paste into editor")],
                                    [("x", "draft from the snippet"), ("q", "cancel")]))
            choice = ui.key("[accent]›[/]", "ocexq")
            if choice == "o":
                self.open_url(job.url)
            elif choice == "x":
                return True
            elif choice == "q":
                return False
            else:
                extracted = self._read_ad(choice == "e")
                if extracted:
                    paste.to_job(job.url, extracted, base=job)
                    self.store.save_job(job, "ranked")
                    ui.ok(f"Got the full ad ({len(job.description):,} characters).")
                    return True

    def _read_ad(self, editor: bool) -> paste.ExtractedJob | None:
        if editor:
            extracted = paste.parse_job_template(ui.edit(paste.JOB_TEMPLATE) or "")
            if not extracted.found_job:
                ui.warn("Needs a title and the ad text.")
                return None
            return extracted
        text = clipboard.paste()
        if not text or len(text.strip()) < MIN_PASTE_CHARS:
            ui.warn("The clipboard doesn't hold a job ad yet. Copy the whole ad page first.")
            return None
        with console.status("Reading the ad from your clipboard…"):
            extracted = paste.extract_job(self.llm, text)
        if not extracted.found_job:
            ui.warn("That doesn't look like a job ad. Copy the ad page again, or press [accent]e[/].")
            return None
        return extracted

    def _draft(self, ranked: RankedJob, instruction: str = "", previous: Application | None = None) -> Drafts | None:
        with console.status("Tailoring your resume and cover letter…"):
            try:
                result = self.writer.draft(ranked.job, ranked, instruction, previous)
            except LLMError as e:
                ui.error(str(e))
                return None
        return Drafts.of(result)

    def _drafts_screen(self, ranked: RankedJob, d: Drafts, single: bool) -> Outcome:
        job = ranked.job
        while True:
            self._show_drafts(d)
            keys = [("e", "edit resume"), ("c", "edit letter"), ("r", "rewrite"), ("x", "export")]
            nav = [("a", "mark applied")] + ([] if single else [("n", "next"), ("b", "back")]) + [("q", "quit")]
            console.print(ui.keybar(keys, nav))
            choice = ui.key("[accent]›[/]", "ecrxaq" + ("" if single else "nb"))
            if choice == "e":
                edited = ui.edit(d.resume)
                if edited is not None:
                    d.resume, d.exported = edited.strip(), None
            elif choice == "c":
                edited = ui.edit(d.letter)
                if edited is not None:
                    d.letter, d.exported = edited.strip(), None
            elif choice == "r":
                instruction = ui.ask("What should change? (e.g. \"lead with the data platform work\")")
                if instruction.strip():
                    new = self._draft(ranked, instruction.strip(), d.current)
                    if new:
                        d = self._drafts[job.key] = new
            elif choice == "x":
                self._export(job, d)
            elif choice == "a":
                if d.exported is None and not ui.confirm("You haven't exported this version. Mark applied anyway?"):
                    continue
                self.store.set_job_status(job.key, "applied")
                self.open_url(job.url)
                ui.ok("Marked as applied, and opened the ad so you can apply.")
                return Outcome.DONE
            elif choice == "n":
                return Outcome.NEXT
            elif choice == "b":
                return Outcome.BACK
            elif choice == "q":
                return Outcome.QUIT

    def _export(self, job: Job, d: Drafts) -> None:
        blockers = export_blockers(d.resume, d.letter)
        if blockers:
            ui.warn(f"Can't export yet: {escape('; '.join(blockers))}. "
                    "Press [accent]e[/] or [accent]c[/] to edit.")
            return
        folder = exporter.folder_for(self.output_root, job)
        files = exporter.export(folder, job, d.resume, d.letter)
        d.exported = folder
        self.store.set_job_status(job.key, "drafted", folder=str(folder))
        ui.ok(f"Saved to {escape(str(folder))}/")
        for f in files:
            ui.muted(f"  {f.name}")

    # ------------------------------------------------------------ display
    def _show_job(self, ranked: RankedJob) -> None:
        job = ranked.job
        console.print(Text(job.title, style="bold"))
        rows = [("Company", Text(job.company or "—")), ("Where", Text(job.location or "—"))]
        if job.salary():
            rows.append(("Salary", Text(job.salary())))
        if job.work_type:
            rows.append(("Type", Text(job.work_type)))
        rows.append(("Posted", Text(f"{job.age_days(now_utc()):.0f} days ago" if job.source == "adzuna" else "—")))
        if ranked.fit or ranked.angle:
            rows.append(("Fit", Text.assemble(f"{ranked.fit}/10  ", ui.bar(ranked.fit / 10, 10))))
        if ranked.angle:
            rows.append(("Lead with", Text(ranked.angle, style="italic")))
        if ranked.gaps:
            rows.append(("Gaps", Text("\n".join(f"• {g}" for g in ranked.gaps), style="warn")))
        rows.append(("Link", Text(job.url, style=f"link {job.url}")))
        ui.meta(rows)
        text = job.description if len(job.description) <= AD_PREVIEW else job.description[:AD_PREVIEW] + "…"
        title = "Ad (start only: from Adzuna)" if job.snippet_only else "Ad"
        console.print(Panel(Text(text), title=title, title_align="left", box=box.ROUNDED, border_style="muted",
                            padding=(0, 1)))

    def _show_drafts(self, d: Drafts) -> None:
        app = d.result.application
        for title, body in (("Resume", d.resume), ("Cover letter", d.letter)):
            n = placeholders(body)
            note = f" · {n} placeholder{'s' if n > 1 else ''} to fill" if n else ""
            console.print(Panel(Markdown(body), title=f"{title}{note}", title_align="left", box=box.ROUNDED,
                                border_style="warn" if n else "accent", padding=(0, 1)))
        if app.matched:
            ui.heading("Matched", f"{len(app.matched)}")
            for m in app.matched:
                console.print(Text.assemble(("  ✓ ", "ok"), (m.requirement, "bold"), ("  ", ""), (m.evidence, "muted")))
        if app.gaps:
            ui.heading("Gaps")
            for g in app.gaps:
                console.print(Text.assemble(("  • ", "warn"), g))
        if app.claims_to_verify:
            ui.heading("Check before sending")
            for c in app.claims_to_verify:
                console.print(Text.assemble(("  ? ", "muted"), c))
        if d.result.sources:
            ui.muted("  Drew on: " + ", ".join(d.result.sources))
        if d.exported:
            ui.muted(f"  Exported to {d.exported}/")
