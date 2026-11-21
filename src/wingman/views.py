# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Presentation of reports (profile, scan results, stats, cost, quality, queue). Commands fetch data and call these."""

from __future__ import annotations

import re
from datetime import datetime

from rich import box
from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text

from wingman import ui
from wingman.core import privacy
from wingman.core import quality as q
from wingman.core import stats as st
from wingman.core.models import ActivityItem, RankedThread, now_utc
from wingman.core.usage import Summary, short_model_name
from wingman.profile.voice import Profile
from wingman.ui import console

TIER_STYLE = {"expert": "accent", "contributor": "ok", "interested": "muted"}


def profile(p: Profile, path: str = "", private_terms: list[str] | None = None) -> None:
    """Your profile; private_terms are masked on screen too (they're also never sent to the drafting model)."""
    if private_terms:
        p = Profile.model_validate_json(privacy.redact(p.model_dump_json(), private_terms))
    ui.heading("Profile", path)
    console.print(Panel(Text(p.summary), box=box.ROUNDED, border_style="muted", padding=(0, 1)))

    topics = ui.table(None, "Topic", "Level")
    for tier in ("expert", "contributor", "interested"):
        for t in (t for t in p.topics if t.tier == tier):
            topics.add_row(Text(t.name), Text(tier, style=TIER_STYLE[tier]))
    console.print(topics)

    ui.heading("Opinions", f"{len(p.opinions)}")
    for n, opinion in enumerate(p.opinions, 1):
        console.print(Text.assemble((f"{n:>3}  ", "muted"), opinion))

    v = p.voice
    ui.heading("Voice")
    ui.meta([("Tone", Text(v.tone)), ("Length", Text(v.typical_length)),
             ("Habits", Text("\n".join(f"• {h}" for h in v.habits))),
             ("Avoids", Text("\n".join(f"• {a}" for a in v.avoid))),
             ("Sounds", Text("\n".join(f"“{s}”" for s in v.example_sentences), style="muted"))])
    ui.heading("Blog posts", f"{len(p.blog_posts)} linkable")
    for b in p.blog_posts:
        console.print(Text.assemble(("  • ", "muted"), (b.title, f"link {b.url}")))
    console.print()


def _snippet(text: str, limit: int) -> str:
    """Whitespace collapsed, light markdown stripped, cut at a word boundary."""
    text = re.sub(r"\s+", " ", re.sub(r"[*_`#>]+", "", text)).strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:—-") + "…"


ROW_LINES = 6  # rough screen lines per opportunity (meta + title + 3-line preview + gap), for page sizing


def _post_cell(r: RankedThread, now: datetime) -> Group:
    """Where/age line, then the post: title plus a short preview of the body (or just the text, if untitled)."""
    t = r.thread
    title, body = _snippet(t.title, 120), _snippet(t.body, 200)
    replies = f"{t.num_comments} repl{'y' if t.num_comments == 1 else 'ies'}"
    lines = [Text.assemble((t.source, "accent"), (f"  ·  {t.age_hours(now):.0f}h ago  ·  {replies}", "muted"))]
    if body.startswith(title.rstrip("…")):  # no separate title (e.g. Bluesky): the text is the post
        lines.append(Text(body, style=f"link {t.url}"))
    else:
        lines.append(Text(title, style=f"bold link {t.url}"))
        if body:
            lines.append(Text(body, style="muted"))
    lines.append(Text())
    return Group(*lines)


def _opportunities_table(rows: list[RankedThread], first: int, now: datetime):
    t = ui.table(None, "#", "Score", "Fit", "Post", "Angle", right=("#", "Fit"))
    t.expand = True
    t.columns[3].ratio, t.columns[4].ratio = 3, 2  # Post and Angle share the width that's left
    for n, r in enumerate(rows, first):
        t.add_row(str(n), Group(Text(f"{r.final:.2f}"), ui.bar(r.final, 8)), f"{r.relevance:.0f}/10",
                  _post_cell(r, now), Text(r.angle, style="italic"))
    return t


def scan_results(found: int, fresh: int, ranked: list[RankedThread], show: int, rerank: bool = False,
                 page_size: int | None = None) -> None:
    label = "to rank" if rerank else "new"
    summary = f"Found {found} posts · {fresh} {label} · ranked {len(ranked)}"
    ui.ok(summary)
    if not fresh and found and not rerank:
        ui.muted("  All of these were ranked in an earlier scan. Run [accent]wingman review[/] to go through them, "
                 "or [accent]wingman scan --rerank[/] to score them again.")
    top = sorted(ranked, key=lambda r: r.final, reverse=True)[:show]
    if not top:
        return
    now = now_utc()
    size = page_size or max(2, (console.height - 8) // ROW_LINES)
    pages = [top[i:i + size] for i in range(0, len(top), size)]
    if len(pages) == 1 or not ui.interactive():
        ui.heading("Best opportunities", f"{len(top)}")
        console.print(_opportunities_table(top, 1, now))
        ui.info("Next: [accent]wingman review[/] to draft and post the best ones. Posts are links.")
        return

    page = 0
    while True:
        console.clear()
        ui.ok(summary)
        ui.heading("Best opportunities", f"page {page + 1} of {len(pages)} · {len(top)} posts")
        console.print(_opportunities_table(pages[page], page * size + 1, now))
        keys = [("n", "next")] * (page < len(pages) - 1) + [("p", "previous")] * (page > 0)
        console.print(ui.keybar(keys, [("q", "done")]))
        choice = ui.key("", "npq" + " ", default="n" if page < len(pages) - 1 else "q")
        if choice == "q" or (choice in "n " and page == len(pages) - 1):
            break
        page = max(0, page - 1) if choice == "p" else page + 1
    ui.info("Next: [accent]wingman review[/] to draft and post the best ones. Posts are links.")


def _breakdown(b: st.Breakdown) -> None:
    numeric = tuple(h for n, h in enumerate(b.headers)
                    if n and all(isinstance(r[n], int | float) or str(r[n]).replace(".", "", 1).isdigit()
                                 for r in b.rows))
    t = ui.table(b.title, *b.headers, right=numeric)
    for r in b.rows:
        t.add_row(*[Text(str(x)) for x in r])
    console.print(t)


def activity(items: list[ActivityItem]) -> None:
    """Where and when you post, for platforms without vote counts."""
    ui.heading("Your activity", f"{len(items)} posts")
    ui.muted("Vote counts aren't available in this mode, so this shows where and when you post.")
    for b in st.activity_breakdowns(items):
        _breakdown(b)


def stats(s: st.StatsSummary) -> None:
    ui.heading("Your activity", f"{s.count} posts")
    ui.meta([("Total", f"{s.total}"), ("Average", f"{s.average:.1f}"), ("Median", f"{s.median:.0f}")])
    for b in s.breakdowns:
        _breakdown(b)
    if s.wingman:
        n, avg = s.wingman
        ui.meta([("Wingman", f"{n} post{'s' if n != 1 else ''} · average {avg:.1f}")])
    _breakdown(st.Breakdown("Top comments", ["community", "score", "excerpt"],
                            [(i.source, i.score, i.body[:80].replace("\n", " ")) for i in s.top]))


def learnings(markdown: str) -> None:
    console.print(Markdown(markdown))


def cost(s: Summary) -> None:
    ui.heading("Claude usage", f"{s.requests} requests")
    ui.meta([("Today", Text(f"${s.today:.2f}", style="title")), ("Month", Text(f"${s.month:.2f}", style="title")),
             ("All time", Text(f"${s.all_time:.2f}", style="title"))])
    console.print()
    t = ui.table(None, "Task", "Model", "Requests", "Avg in", "Avg out", "Avg cost", "Total",
                 right=("Requests", "Avg in", "Avg out", "Avg cost", "Total"))
    for row in s.by_task:
        t.add_row(row.task, short_model_name(row.model), str(row.requests), f"{row.avg_input / 1000:.1f}K",
                  f"{row.avg_output / 1000:.1f}K", f"${row.total_cost / row.requests:.3f}", f"${row.total_cost:.2f}")
    console.print(t)
    ui.muted("Estimates from list prices. Your real balance: platform.claude.com")


def quality(s: q.QualitySummary) -> None:
    ui.heading("Draft quality", f"{s.count} rated threads")
    kept = f"{s.avg_similarity:.0%}" if s.avg_similarity is not None else "-"
    ui.meta([("Average", Text(f"{s.avg_rating:.1f} / 5", style="title")), ("Posted", Text(f"{s.posted_pct:.0f}%")),
             ("Kept", Text(f"{kept} of each draft, on average, when posted")),
             ("Rewrites", Text(f"{s.avg_regenerations:.1f} per thread"))])
    console.print()
    t = ui.table("Ratings", "", "Meaning", "", "Threads", right=("Threads",))
    for score, n in s.distribution.items():
        t.add_row(str(score), q.SCALE[score], ui.bar(n / s.count, 16), str(n))
    console.print(t)
    for title, groups in (("By model", s.by_model), ("By platform", s.by_platform)):
        if title == "By platform" and len(groups) < 2:
            continue
        g_table = ui.table(title, "", "Threads", "Avg", "Posted", "Kept", right=("Threads", "Avg", "Posted", "Kept"))
        for g in groups:
            name = short_model_name(g.name) if g.name.startswith("claude-") else g.name
            kept_g = f"{g.avg_similarity:.0%}" if g.avg_similarity is not None else "-"
            g_table.add_row(name, str(g.count), f"{g.avg_rating:.1f}", f"{g.posted_pct:.0f}%", kept_g)
        console.print(g_table)
    if s.issues:
        ui.meta([("Issues", Text(" · ".join(f"{issue} ×{n}" for issue, n in s.issues), style="warn"))])
    if s.count < 20:
        ui.muted(f"Only {s.count} ratings so far; the numbers settle after about 20–30 threads.")


def queue(rows: list[dict]) -> None:
    ui.heading("Queued replies", f"{len(rows)}")
    t = ui.table(None, "ID", "Where", "Due", "Reply", right=("ID",))
    for r in rows:
        due = datetime.fromisoformat(r["due_at"]).astimezone().strftime("%a %H:%M") if r["due_at"] else "-"
        t.add_row(str(r["id"]), Text(f"{r['platform']} · {r['source']}", "accent"), due, Text(r["text"][:80]))
    console.print(t)


def job_results(found: int, fresh: int, ranked: list, show: int, attribution: str = "") -> None:
    """`wingman jobs scan`: the best-fitting new jobs."""
    from wingman.jobs.models import RankedJob

    ui.ok(f"Found {found} jobs · {fresh} new · ranked {len(ranked)}")
    if not fresh and found:
        ui.muted("  All of these were ranked before. Run [accent]wingman jobs review[/] to go through them.")
    top: list[RankedJob] = sorted(ranked, key=lambda r: r.fit, reverse=True)[:show]
    if top:
        now = now_utc()
        ui.heading("Best matches", f"{len(top)}")
        t = ui.table(None, "#", "Fit", "Job", "Lead with / gaps", right=("#",))
        t.expand = True
        t.columns[2].ratio, t.columns[3].ratio = 3, 2
        for n, r in enumerate(top, 1):
            j = r.job
            where = " · ".join(p for p in (j.company, j.location, j.salary(), f"{j.age_days(now):.0f}d ago") if p)
            job_cell = Group(Text(_snippet(j.title, 100), style=f"bold link {j.url}"), Text(where, style="muted"),
                             Text())
            notes = Group(Text(r.angle, style="italic"),
                          *([Text("gaps: " + "; ".join(r.gaps), style="warn")] if r.gaps else []))
            t.add_row(str(n), Group(Text(f"{r.fit}/10"), ui.bar(r.fit / 10, 6)), job_cell, notes)
        console.print(t)
        ui.info("Next: [accent]wingman jobs review[/] to draft a tailored resume and cover letter. Titles are links.")
    if attribution:
        ui.muted(f"  {attribution}")
