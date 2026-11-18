"""wingman: a human-in-the-loop copilot for online communities.

Commands are thin: they gather input, call core/sources/services, and print results.
Errors derived from WingmanError are shown as one red line (see `main`).
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.logging import RichHandler
from rich.prompt import Confirm, Prompt
from rich.table import Table

from wingman.core.models import Reply, now_utc
from wingman.errors import NotReadyError, WingmanError
from wingman.services import Services
from wingman.ui import console

app = typer.Typer(help="Finds threads you can add to and drafts replies in your voice. You approve every post.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False)
profile_app = typer.Typer(help="Build or view your interest/voice profile.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

PlatformOpt = typer.Option("reddit", "--platform", "-p", help="Which platform to use.")


def _services() -> Services:
    return Services(on_usage=lambda line: console.print(f"[dim]  {line}[/]"))


def _progress(message: str) -> None:
    console.print(f"  {message}")


# ---------------------------------------------------------------- setup


@app.command()
def init():
    """Create wingman.toml and .env from the examples."""
    home = Path.cwd()
    for example, target in (("wingman.example.toml", "wingman.toml"), (".env.example", ".env")):
        src, dst = home / example, home / target
        if dst.exists():
            console.print(f"{target} already exists, leaving it alone.")
        elif src.exists():
            shutil.copy(src, dst)
            console.print(f"[green]Created {target}[/]")
    console.print("Next: fill in .env, set blog_url and subreddits in wingman.toml, then run "
                  "[bold]wingman profile build[/].")


@app.command()
def check(platform: str = PlatformOpt):
    """Test that wingman can read threads and your comments (prints status only, never tokens)."""
    svc = _services()
    plat = svc.platform(platform)
    threads = []
    try:
        with console.status("Reading threads…"):
            threads = plat.discover(svc.cfg.platform_settings(platform).get("sources", []),
                                    datetime(2000, 1, 1, tzinfo=UTC), 10)
        console.print(f"[green]✓ threads:[/] {len(threads)} from "
                      f"{', '.join(sorted({t.source for t in threads})) or 'nothing'}")
    except WingmanError as e:
        console.print(f"[yellow]✗ threads:[/] {e}")
    if threads:
        try:
            t = plat.fetch_context(threads[0])
            console.print(f"[green]✓ replies:[/] {len(t.top_comments)} on \"{t.title[:60]}\"")
        except Exception as e:  # noqa: BLE001 - report any adapter failure
            console.print(f"[yellow]✗ replies:[/] {e} (review still works; drafts just won't see them)")
    try:
        console.print(f"[green]✓ your comments:[/] {len(plat.my_activity(limit=25))}")
    except WingmanError as e:
        console.print(f"[yellow]✗ your comments:[/] {e}")


@profile_app.command("build")
def profile_build(
    blog: str = typer.Option(None, help="Blog URL (overrides wingman.toml for this run)."),
    platform: str = typer.Option("reddit", "--platform", "-p", help="Also learn from your comments here."),
    no_comments: bool = typer.Option(False, help="Don't read your past comments."),
    notes: str = typer.Option("", help="Anything else about you, e.g. '10 years in Python, opinions on testing'."),
):
    """Read your blog, background pages/files (and past comments) and build your profile."""
    from wingman import sources
    from wingman.profile.voice import build_profile

    svc = _services()
    if blog:
        svc.settings.profile.blog_url = blog
    collected = sources.collect(svc.cfg, max_posts=svc.settings.profile.blog_max_pages, include_files=True,
                                progress=_progress)
    for skipped in collected.skipped:
        console.print(f"[yellow]  skipped {skipped}[/]")

    comments = []
    if not no_comments:
        try:
            comments = svc.platform(platform).my_activity(limit=300)
            svc.store.save_activity(comments)
            if comments:
                console.print(f"  {len(comments)} of your comments")
        except WingmanError as e:
            console.print(f"[yellow]  skipped past comments: {e}[/]")
    if not collected.all and not comments and not notes:
        raise WingmanError("Nothing to build a profile from. Set blog_url, pages or files, or pass --notes.")

    with console.status("Building your profile with Claude…"):
        profile = build_profile(svc.llm, collected.blog, collected.background, comments, notes)
    path = svc.profiles.save(profile)
    console.print(profile.brief())
    console.print(f"\n[green]Saved to {path}[/] (edit it freely; wingman reads it on every run).")


@profile_app.command("show")
def profile_show():
    """Print your current profile."""
    console.print(_services().profile().brief())


@app.command()
def index():
    """Index your own writing (blog, GitHub READMEs, pages, posted replies) for drafts. Free: no Claude calls."""
    from wingman import sources
    from wingman.core.models import Document
    from wingman.core.retrieval import Index

    svc = _services()
    collected = sources.collect(svc.cfg, max_posts=svc.settings.drafting.index_max_posts, include_files=False,
                                progress=_progress)
    for skipped in collected.skipped:
        console.print(f"[yellow]  skipped {skipped}[/]")
    # The bare GitHub profile entry ("N public repos") adds nothing useful to drafts.
    docs = [d for d in collected.all if not d.title.startswith("GitHub profile:")]
    replies = [r for name in svc.settings.platforms for r in svc.store.logged_posts(name)]
    docs += [Document(title=f"Your reply in {r.source}", text=r.body, kind="reply", url=r.permalink,
                      date=r.created_at.date().isoformat()) for r in replies]
    if replies:
        _progress(f"{len(replies)} of your posted replies")
    if not docs:
        raise WingmanError("Nothing to index. Set blog_url, github or pages in wingman.toml.")

    embedder = svc.embedder()
    with console.status(f"Indexing {len(docs)} sources" + (f" with {embedder.model} (local)…" if embedder else "…")):
        idx = Index.build(docs, embedder, svc.weighting)
    idx.save(svc.index_path)
    console.print(f"[green]Indexed {len(idx.chunks)} chunks from {len(docs)} sources · search: {idx.mode}[/]")
    if embedder and not idx.embedder:
        console.print(f"[dim]Ollama wasn't reachable (or `{embedder.model}` isn't pulled), so this index is "
                      f"keyword-only. Start Ollama, run `ollama pull {embedder.model}`, then `wingman index` again.[/]")


# ---------------------------------------------------------------- drafting & posting


def _thread_from_paste(svc: Services, platform: str, url: str, editor: bool):
    """Build a thread from text you copied (or typed into the editor template)."""
    from wingman.core import clipboard
    from wingman.core.paste import EDITOR_TEMPLATE, extract_from_text, parse_template, parse_url, to_thread

    community, thread_id = parse_url(url)
    extracted = None
    clip = None if editor else clipboard.paste()
    if clip and len(clip.strip()) >= 80 and clip.strip() != url:
        preview = " ".join(clip.split())[:90]
        if Confirm.ask(f"Use the copied text ({len(clip):,} chars: “{preview}…”)?", default=True):
            with console.status("Reading the thread from your clipboard…"):
                extracted = extract_from_text(svc.llm, clip)
            if not extracted.found_thread:
                console.print("[yellow]That doesn't look like a thread. Paste it into the editor instead.[/]")
                extracted = None
    if extracted is None:
        extracted = parse_template(typer.edit(EDITOR_TEMPLATE, extension=".md") or "")
        if not extracted.found_thread:
            raise WingmanError("No thread text provided.")
    community = community or extracted.community or Prompt.ask("Which community is this in?")
    return to_thread(platform, url, community.removeprefix("r/"), thread_id, extracted)


@app.command()
def draft(
    url: str = typer.Argument(None, help="The thread's URL (prompted if omitted)."),
    platform: str = typer.Option(None, "--platform", "-p", help="Which platform (default: from the URL)."),
    editor: bool = typer.Option(False, "--editor", help="Paste the thread into your editor instead of "
                                                        "reading it from the clipboard."),
):
    """Draft a reply to a thread you found yourself.

    Bluesky: just pass the post's link. Reddit (paste mode): on the thread page, select all (Cmd+A)
    and copy (Cmd+C) first, then run `wingman draft <url>`.
    """
    from wingman.core.review import ReviewSession
    from wingman.platforms import platform_for_url

    url = (url or Prompt.ask("Thread URL")).strip()
    platform = platform or platform_for_url(url) or "reddit"
    svc = _services()
    plat = svc.platform(platform)
    drafter = svc.drafter(plat)  # fails early (before any Claude call) if there's no profile yet

    with console.status("Loading the thread…"):
        thread = plat.thread_from_url(url)
    if thread is None:
        thread = _thread_from_paste(svc, platform, url, editor)
    if svc.store.thread_status(thread.key) == "posted" and \
            not Confirm.ask("You've already replied here. Continue?", default=False):
        raise typer.Exit()
    svc.store.save_pasted(thread)
    console.print(f"[dim]Read: {len(thread.top_comments)} replies, "
                  f"{len(thread.source_rules) or 'no'} community rules.[/]")
    ReviewSession(plat, svc.store, drafter, svc.settings.pacing, svc.settings.profile.private_terms,
                  max_chars=svc.max_post_chars(plat), ask_rating=svc.settings.drafting.ask_rating) \
        .review(thread, single=True)


@app.command()
def post(
    topic: str = typer.Argument(None, help="What to post about (prompted if omitted, unless --blog is given)."),
    blog: str = typer.Option(None, "--blog", help="Share one of your blog posts: its URL."),
    notes: str = typer.Option("", help="Extra points you want in the post."),
    platform: str = typer.Option("bluesky", "--platform", "-p", help="Where to post."),
):
    """Draft a new post in your voice (not a reply): about a topic, or sharing a blog post.

    Same review keys as replies: pick, edit, regenerate, then p to publish after you confirm.
    """
    from wingman.core.composer import compose_target
    from wingman.core.review import ReviewSession
    from wingman.sources.web import fetch_article

    svc = _services()
    plat = svc.platform(platform)
    if plat.posting != "api":
        raise WingmanError(f"New posts need a platform wingman can post to; {platform} isn't one in this mode.")

    article = link = None
    if blog:
        with console.status("Reading the article…"):
            article, link = fetch_article(blog)
        topic = topic or link.title
    topic = (topic or Prompt.ask("What do you want to post about?")).strip()
    if not topic:
        raise WingmanError("Nothing to post about.")

    composer = svc.composer(plat, article)  # fails early if there's no profile yet
    target = compose_target(platform, topic, notes, url=blog or "")
    svc.store.save_pasted(target)
    ReviewSession(plat, svc.store, composer, svc.settings.pacing, svc.settings.profile.private_terms,
                  max_chars=svc.max_post_chars(plat), ask_rating=svc.settings.drafting.ask_rating,
                  new_post=True, link=link).review(target, single=True)


@app.command()
def scan(platform: str = PlatformOpt, show: int = typer.Option(10, help="How many top results to list.")):
    """Fetch new/rising threads and rank them against your profile (API mode)."""
    from wingman.core.ranker import rank

    svc = _services()
    plat = svc.platform(platform)
    settings = svc.cfg.platform_settings(platform)
    profile_brief = svc.profile().brief()

    now = now_utc()
    since = now - timedelta(hours=settings.get("max_age_hours", 12))
    with console.status(f"Scanning {', '.join(settings['sources'])}…"):
        threads = plat.discover(settings["sources"], since, settings.get("per_source_limit", 25))
    known = svc.store.known_keys(platform)
    fresh = [t for t in threads if t.key not in known]
    console.print(f"Found {len(threads)} threads, {len(fresh)} new.")
    if not fresh:
        return
    ranked = rank(svc.llm, profile_brief, settings.get("keywords", []), fresh, now, progress=_progress)
    for r in ranked:
        svc.store.save_ranked(r)

    table = Table(title="Top opportunities", title_justify="left")
    for col in ("score", "rel", "opp", "community", "title", "angle"):
        table.add_column(col, overflow="fold")
    for r in sorted(ranked, key=lambda r: r.final, reverse=True)[:show]:
        table.add_row(f"{r.final:.2f}", str(r.relevance), f"{r.opportunity:.2f}", r.thread.source,
                      r.thread.title[:70], r.angle[:80])
    console.print(table)
    console.print("Run [bold]wingman review[/] to go through them.")


@app.command()
def review(
    platform: str = PlatformOpt,
    limit: int = typer.Option(10, help="Max threads to review this session."),
    min_score: float = typer.Option(0.5, help="Only threads with a combined score at or above this (0-1)."),
):
    """Go through the best scanned threads, pick/edit a draft, and post what you approve."""
    from wingman.core.review import ReviewSession

    svc = _services()
    plat = svc.platform(platform)
    items = svc.store.review_queue(platform, min_score, svc.cfg.platform_settings(platform).get("max_age_hours", 12),
                                   limit)
    ReviewSession(plat, svc.store, svc.drafter(plat, batch=True), svc.settings.pacing,
                  svc.settings.profile.private_terms, max_chars=svc.max_post_chars(plat),
                  ask_rating=svc.settings.drafting.ask_rating).run(items)


@app.command()
def queue(platform: str = typer.Option(None, "--platform", "-p")):
    """List approved replies waiting for their pacing slot (API mode)."""
    rows = _services().store.queued_posts(platform)
    if not rows:
        console.print("Queue is empty.")
        return
    table = Table()
    for col in ("id", "platform", "community", "due", "text"):
        table.add_column(col, overflow="fold")
    for r in rows:
        due = datetime.fromisoformat(r["due_at"]).astimezone().strftime("%a %H:%M") if r["due_at"] else "-"
        table.add_row(str(r["id"]), r["platform"], r["source"], due, r["text"][:80])
    console.print(table)


@app.command()
def flush(platform: str = PlatformOpt):
    """Post queued replies whose time has come (you already approved them)."""
    from wingman.core.pacing import next_allowed

    svc = _services()
    due = [r for r in svc.store.queued_posts(platform)
           if r["due_at"] and datetime.fromisoformat(r["due_at"]) <= now_utc()]
    if not due:
        console.print("Nothing due yet.")
        return
    plat = svc.platform(platform)
    for r in due:
        if next_allowed(svc.store, platform, r["source"], svc.settings.pacing):
            console.print("Pacing limit reached; run flush again later.")
            break
        console.print(f"[bold]{r['source']}[/]: {r['text'][:120]}")
        if not Confirm.ask("Post it?", default=True):
            continue
        try:
            post_url = plat.submit(Reply(platform, r["thread_key"].split(":", 1)[1], r["source"], r["text"]))
        except Exception as e:  # noqa: BLE001 - record any platform failure and continue with the rest
            svc.store.mark_post(r["id"], "failed", error=str(e))
            console.print(f"[red]Failed:[/] {e}")
            continue
        svc.store.mark_post(r["id"], "posted", url=post_url)
        svc.store.set_status(r["thread_key"], "posted")
        console.print(f"[green]Posted:[/] {post_url}")


# ---------------------------------------------------------------- reporting


@app.command()
def stats(
    platform: str = PlatformOpt,
    insights: bool = typer.Option(False, help="Ask Claude what makes your best comments work; "
                                              "the result feeds future drafts."),
):
    """Your activity (API mode: karma breakdown by community, hour and length)."""
    from wingman.core.stats import show_activity, show_stats, write_learnings

    svc = _services()
    plat = svc.platform(platform)
    try:
        with console.status("Fetching your comments…"):
            svc.store.save_activity(plat.my_activity(limit=1000))
    except WingmanError as e:
        console.print(f"[yellow]Could not refresh from {platform} ({e}); showing saved data.[/]")
    items = svc.store.activity(platform)
    if not plat.has_scores:
        if not items:
            items = svc.store.logged_posts(platform)
            console.print("[dim]Showing replies you've posted through wingman.[/]")
        show_activity(items)
        if insights:
            console.print("[yellow]--insights needs vote counts, which require Reddit API access.[/]")
        return
    show_stats(items, svc.store.wingman_post_urls())
    if insights and items:
        with console.status("Analysing what works…"):
            path = write_learnings(svc.llm, items, svc.cfg.data_dir)
        console.print(f"\n{path.read_text()}\n[green]Saved to {path}; drafts will use it from now on.[/]")


@app.command()
def cost():
    """What wingman has spent on Claude, by task and model (estimates; the Console is authoritative)."""
    from wingman.core.usage import summarize

    entries = _services().usage_log.entries()
    if not entries:
        console.print("No usage recorded yet.")
        return
    s = summarize(entries, datetime.now(UTC).astimezone())
    console.print(f"[bold]Today[/] ${s.today:.2f} · [bold]This month[/] ${s.month:.2f} · "
                  f"[bold]All time[/] ${s.all_time:.2f}  ({s.requests} requests)")
    table = Table(title="By task", title_justify="left")
    for col in ("task", "model", "requests", "avg in", "avg out", "avg cost", "total"):
        table.add_column(col, justify="left" if col in ("task", "model") else "right")
    for t in s.by_task:
        table.add_row(t.task, t.model, str(t.requests), f"{t.avg_input / 1000:.1f}K", f"{t.avg_output / 1000:.1f}K",
                      f"${t.total_cost / t.requests:.3f}", f"${t.total_cost:.2f}")
    console.print(table)
    console.print("[dim]Estimates from list prices; see platform.claude.com for your actual balance.[/]")


@app.command("quality")
def quality_report(platform: str = typer.Option(None, "--platform", "-p", help="Only this platform.")):
    """How good the drafts are, from your ratings: by model, by platform, and what tends to be off."""
    from wingman.core import quality

    s = quality.summarize(_services().store.ratings(platform))
    if s is None:
        console.print("No ratings yet. After each thread in `wingman draft` / `review`, rate the drafts 1-5.")
        return
    sim = f" · drafts kept {s.avg_similarity:.0%} intact on average when posted" if s.avg_similarity is not None else ""
    console.print(f"[bold]{s.count} rated threads[/] · average [bold]{s.avg_rating:.1f}/5[/] · "
                  f"{s.posted_pct:.0f}% posted{sim} · {s.avg_regenerations:.1f} regenerations per thread")

    table = Table(title="Ratings", title_justify="left")
    for col in ("rating", "meaning", "threads", "share"):
        table.add_column(col, justify="left" if col == "meaning" else "right")
    for score, n in s.distribution.items():
        table.add_row(str(score), quality.SCALE[score], str(n), f"{100 * n / s.count:.0f}%")
    console.print(table)

    for title, groups in (("By model", s.by_model), ("By platform", s.by_platform)):
        if len(groups) < 2 and title == "By platform":
            continue
        t = Table(title=title, title_justify="left")
        for col in ("", "threads", "avg rating", "posted", "kept intact"):
            t.add_column(col, justify="left" if not col else "right")
        for g in groups:
            kept = f"{g.avg_similarity:.0%}" if g.avg_similarity is not None else "-"
            t.add_row(g.name, str(g.count), f"{g.avg_rating:.1f}", f"{g.posted_pct:.0f}%", kept)
        console.print(t)
    if s.issues:
        console.print("[bold]What was off[/] (ratings of 3 or lower): "
                      + ", ".join(f"{issue} ×{n}" for issue, n in s.issues))
    if s.count < 20:
        console.print(f"[dim]Only {s.count} ratings so far; the numbers settle after about 20-30 threads.[/]")


# ---------------------------------------------------------------- entry point


def main() -> None:
    """Console entry point: user-facing errors print as one line instead of a traceback."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s",
                        handlers=[RichHandler(console=console, show_time=False, show_path=False)])
    try:
        app()
    except NotReadyError as e:
        console.print(f"[yellow]{e}[/]")
        raise SystemExit(1) from None
    except WingmanError as e:
        console.print(f"[red]{e}[/]")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
