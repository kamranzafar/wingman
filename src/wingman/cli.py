"""wingman: a human-in-the-loop copilot for online communities.

Commands are thin: they gather input, call core/sources/services, and hand results to `views`.
Errors derived from WingmanError are shown as one line (see `main`).
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import typer
from rich.logging import RichHandler
from rich.markup import escape

from wingman import ui, views
from wingman.core.models import Reply, now_utc
from wingman.errors import ConfigError, NotReadyError, WingmanError
from wingman.services import Services
from wingman.ui import console

app = typer.Typer(help="Finds conversations you can add to and drafts replies in your voice. You approve every post.",
                  no_args_is_help=True, pretty_exceptions_show_locals=False, rich_markup_mode="rich")
profile_app = typer.Typer(help="Build or view your interest/voice profile.", no_args_is_help=True)
app.add_typer(profile_app, name="profile")

PlatformOpt = typer.Option("reddit", "--platform", "-p", help="Which platform to use.")


def _services() -> Services:
    return Services(on_usage=lambda line: ui.muted(f"  ⋯ {line}"))


def _skipped(items: list[str]) -> None:
    for item in items:
        ui.warn(f"Skipped {escape(item)}")


# ---------------------------------------------------------------- setup


@app.command(rich_help_panel="Setup")
def init():
    """Create wingman.toml and .env from the examples."""
    home = Path.cwd()
    for example, target in (("wingman.example.toml", "wingman.toml"), (".env.example", ".env")):
        src, dst = home / example, home / target
        if dst.exists():
            ui.muted(f"{target} already exists, left as is.")
        elif src.exists():
            shutil.copy(src, dst)
            ui.ok(f"Created {target}")
    ui.info("Next: add your keys to [accent].env[/], set blog_url in [accent]wingman.toml[/], "
            "then run [accent]wingman profile build[/].")


@app.command(rich_help_panel="Setup")
def check(platform: str = PlatformOpt):
    """Test that wingman can read posts and your history (prints status only, never tokens)."""
    svc = _services()
    plat = svc.platform(platform)
    ui.heading(f"Checking {platform}")
    threads = []
    try:
        with console.status("Reading posts…"):
            threads = plat.discover(svc.cfg.platform_settings(platform).get("sources", []),
                                    datetime(2000, 1, 1, tzinfo=UTC), 10)
        sources = ", ".join(sorted({t.source for t in threads})) or "nothing"
        ui.ok(f"Posts: {len(threads)} from {escape(sources)}")
    except WingmanError as e:
        ui.error(f"Posts: {escape(str(e))}")
    if threads:
        try:
            t = plat.fetch_context(threads[0])
            ui.ok(f"Replies: {len(t.top_comments)} loaded on “{escape(t.title[:60])}”")
        except Exception as e:  # noqa: BLE001 - report any adapter failure
            ui.warn(f"Replies: {escape(str(e))} (review still works; drafts just won't see them)")
    try:
        ui.ok(f"Your history: {len(plat.my_activity(limit=25))} recent posts")
    except WingmanError as e:
        ui.warn(f"Your history: {escape(str(e))}")


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
    ui.heading("Reading your writing")
    with console.status("Collecting sources…"):
        collected = sources.collect(svc.cfg, max_posts=svc.settings.profile.blog_max_pages, include_files=True,
                                    progress=ui.step)
    _skipped(collected.skipped)

    comments = []
    if not no_comments:
        try:
            comments = svc.platform(platform).my_activity(limit=300)
            svc.store.save_activity(comments)
            if comments:
                ui.step(f"{len(comments)} of your comments")
        except WingmanError as e:
            ui.warn(f"Skipped past comments: {escape(str(e))}")
    if not collected.all and not comments and not notes:
        raise WingmanError("Nothing to build a profile from. Set blog_url, pages or files, or pass --notes.")

    with console.status("Building your profile with Claude…"):
        profile = build_profile(svc.llm, collected.blog, collected.background, comments, notes)
    path = svc.profiles.save(profile)
    views.profile(profile, private_terms=svc.settings.profile.private_terms)
    ui.ok(f"Saved to {path} [muted](edit it freely; wingman reads it on every run)[/]")


@profile_app.command("show")
def profile_show():
    """Show your current profile."""
    svc = _services()
    views.profile(svc.profile(), str(svc.profiles.path), svc.settings.profile.private_terms)


@app.command(rich_help_panel="Setup")
def index():
    """Index your own writing (blog, GitHub READMEs, pages, posted replies) for drafts. Free: no Claude calls."""
    from wingman import sources
    from wingman.core.models import Document
    from wingman.core.retrieval import Index

    svc = _services()
    ui.heading("Indexing your writing")
    with console.status("Collecting sources…"):
        collected = sources.collect(svc.cfg, max_posts=svc.settings.drafting.index_max_posts, include_files=False,
                                    progress=ui.step)
    _skipped(collected.skipped)
    # The bare GitHub profile entry ("N public repos") adds nothing useful to drafts.
    docs = [d for d in collected.all if not d.title.startswith("GitHub profile:")]
    replies = [r for name in svc.settings.platforms for r in svc.store.logged_posts(name)]
    docs += [Document(title=f"Your reply in {r.source}", text=r.body, kind="reply", url=r.permalink,
                      date=r.created_at.date().isoformat()) for r in replies]
    if replies:
        ui.step(f"{len(replies)} of your posted replies")
    if not docs:
        raise WingmanError("Nothing to index. Set blog_url, github or pages in wingman.toml.")

    embedder = svc.embedder()
    with console.status(f"Indexing {len(docs)} sources" + (f" with {embedder.model} (local)…" if embedder else "…")):
        idx = Index.build(docs, embedder, svc.weighting)
    idx.save(svc.index_path)
    ui.ok(f"Indexed {len(idx.chunks)} passages from {len(docs)} sources · search: {idx.mode}")
    if embedder and not idx.embedder:
        ui.warn(f"Ollama wasn't reachable (or [accent]{embedder.model}[/] isn't pulled), so search is keyword-only. "
                f"Start Ollama, run [accent]ollama pull {embedder.model}[/], then index again.")


# ---------------------------------------------------------------- drafting & posting


def _thread_from_paste(svc: Services, platform: str, url: str, editor: bool):
    """Build a thread from text you copied (or typed into the editor template)."""
    from wingman.core import clipboard
    from wingman.core.paste import EDITOR_TEMPLATE, extract_from_text, parse_template, parse_url, to_thread

    community, thread_id = parse_url(url)
    extracted = None
    clip = None if editor else clipboard.paste()
    if clip and len(clip.strip()) >= 80 and clip.strip() != url:
        preview = escape(" ".join(clip.split())[:70])
        if ui.confirm(f"Use the text you copied ({len(clip):,} chars: “{preview}…”)?", default=True):
            with console.status("Reading the thread from your clipboard…"):
                extracted = extract_from_text(svc.llm, clip)
            if not extracted.found_thread:
                ui.warn("That doesn't look like a thread. Paste it into the editor instead.")
                extracted = None
    if extracted is None:
        extracted = parse_template(typer.edit(EDITOR_TEMPLATE, extension=".md") or "")
        if not extracted.found_thread:
            raise WingmanError("No thread text provided.")
    community = community or extracted.community or ui.ask("Which community is this in?")
    return to_thread(platform, url, community.removeprefix("r/"), thread_id, extracted)


@app.command(rich_help_panel="Write")
def draft(
    url: str = typer.Argument(None, help="The thread's URL (prompted if omitted)."),
    platform: str = typer.Option(None, "--platform", "-p", help="Which platform (default: from the URL)."),
    editor: bool = typer.Option(False, "--editor", help="Paste the thread into your editor instead of "
                                                        "reading it from the clipboard."),
):
    """Draft a reply to one post you found yourself.

    Bluesky: just pass the post's link. Reddit (paste mode): on the thread page, select all (Cmd+A)
    and copy (Cmd+C) first, then run `wingman draft <url>`.
    """
    from wingman.core.review import ReviewSession
    from wingman.platforms import platform_for_url

    url = (url or ui.ask("Thread URL")).strip()
    platform = platform or platform_for_url(url) or "reddit"
    svc = _services()
    plat = svc.platform(platform)
    drafter = svc.drafter(plat)  # fails early (before any Claude call) if there's no profile yet

    with console.status("Loading the conversation…"):
        thread = plat.thread_from_url(url)
    if thread is None:
        thread = _thread_from_paste(svc, platform, url, editor)
    if svc.store.thread_status(thread.key) == "posted" and \
            not ui.confirm("You've already replied here. Draft another reply?"):
        raise typer.Exit()
    svc.store.save_pasted(thread)
    ReviewSession(plat, svc.store, drafter, svc.settings.pacing, svc.settings.profile.private_terms,
                  max_chars=svc.max_post_chars(plat), ask_rating=svc.settings.drafting.ask_rating) \
        .review(thread, single=True)


@app.command(rich_help_panel="Write")
def post(
    topic: str = typer.Argument(None, help="What to post about (prompted if omitted, unless --blog is given)."),
    blog: str = typer.Option(None, "--blog", help="Share one of your blog posts: its URL."),
    notes: str = typer.Option("", help="Extra points you want in the post."),
    platform: str = typer.Option("bluesky", "--platform", "-p", help="Where to post."),
):
    """Draft a new post in your voice (not a reply): about a topic, or sharing a blog post."""
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
    topic = (topic or ui.ask("What do you want to post about?")).strip()
    if not topic:
        raise WingmanError("Nothing to post about.")

    composer = svc.composer(plat, article)  # fails early if there's no profile yet
    target = compose_target(platform, topic, notes, url=blog or "")
    svc.store.save_pasted(target)
    ReviewSession(plat, svc.store, composer, svc.settings.pacing, svc.settings.profile.private_terms,
                  max_chars=svc.max_post_chars(plat), ask_rating=svc.settings.drafting.ask_rating,
                  new_post=True, link=link).review(target, single=True)


@app.command(rich_help_panel="Find & review")
def scan(
    platform: str = PlatformOpt,
    show: int = typer.Option(20, help="How many top results to list."),
    page_size: int = typer.Option(None, "--page-size", help="Results per page (default: what fits your terminal)."),
    rerank: bool = typer.Option(False, "--rerank", help="Rank already-seen posts again (e.g. after updating your "
                                                       "profile). Posts you skipped, queued or posted are left alone."),
    topics: list[str] = typer.Option(None, "--topic", "-t", help="Scan this instead of the configured sources "
                                     "(repeat for more): a search term, #hashtag, owner/repo or subreddit, "
                                     "depending on the platform."),
):
    """Find new posts on your topics and rank them against your profile.

    Examples: [accent]wingman scan -p bluesky -t "outbox pattern" -t debezium[/],
    [accent]wingman scan -p mastodon -t "#kafka"[/], [accent]wingman scan -p github -t quarkusio/quarkus[/]
    """
    from wingman.core.ranker import rank

    svc = _services()
    plat = svc.platform(platform)
    settings = svc.cfg.platform_settings(platform)
    profile_brief = svc.profile().brief()

    now = now_utc()
    since = now - timedelta(hours=settings.get("max_age_hours", 12))
    hours = settings.get("max_age_hours", 12)
    sources = [t.strip() for t in topics or [] if t.strip()] or settings.get("sources", [])
    if not sources:
        raise ConfigError(f"Nothing to scan: pass --topic, or set sources in [platforms.{platform}] in wingman.toml.")
    where = ", ".join(sources) if topics else f"{len(sources)} sources"
    ui.heading(f"Scanning {platform}", f"{where} · last {hours}h")
    with console.status("Searching…"):
        threads = plat.discover(sources, since, settings.get("per_source_limit", 25))
    known = svc.store.known_keys(platform, acted_on_only=rerank)
    fresh = [t for t in threads if t.key not in known]
    ranked = []
    if fresh:
        with console.status(f"Ranking {len(fresh)} posts…"):
            ranked = rank(svc.llm, profile_brief, settings.get("keywords", []), fresh, now)
        for r in ranked:
            svc.store.save_ranked(r)
    views.scan_results(len(threads), len(fresh), ranked, show, rerank, page_size)


@app.command(rich_help_panel="Find & review")
def review(
    platform: str = PlatformOpt,
    limit: int = typer.Option(10, help="Max threads to review this session."),
    min_score: float = typer.Option(0.5, help="Only threads with a combined score at or above this (0-1)."),
):
    """Go through the best scanned posts: pick or edit a draft, and post what you approve."""
    from wingman.core.review import ReviewSession

    svc = _services()
    plat = svc.platform(platform)
    items = svc.store.review_queue(platform, min_score, svc.cfg.platform_settings(platform).get("max_age_hours", 12),
                                   limit)
    ReviewSession(plat, svc.store, svc.drafter(plat, batch=True), svc.settings.pacing,
                  svc.settings.profile.private_terms, max_chars=svc.max_post_chars(plat),
                  ask_rating=svc.settings.drafting.ask_rating).run(items)


@app.command(rich_help_panel="Find & review")
def queue(platform: str = typer.Option(None, "--platform", "-p")):
    """List approved replies waiting for their pacing slot."""
    rows = _services().store.queued_posts(platform)
    if not rows:
        ui.info("Nothing queued.")
        return
    views.queue(rows)


@app.command(rich_help_panel="Find & review")
def flush(platform: str = PlatformOpt):
    """Post queued replies whose time has come (you already approved them)."""
    from wingman.core.pacing import next_allowed

    svc = _services()
    due = [r for r in svc.store.queued_posts(platform)
           if r["due_at"] and datetime.fromisoformat(r["due_at"]) <= now_utc()]
    if not due:
        ui.info("Nothing due yet.")
        return
    plat = svc.platform(platform)
    ui.heading("Posting queued replies", f"{len(due)} due")
    for r in due:
        if next_allowed(svc.store, platform, r["source"], svc.settings.pacing):
            ui.warn("You've hit your posting pace; run flush again later.")
            break
        console.print(f"[accent]{escape(r['source'])}[/]  {escape(r['text'][:120])}")
        if not ui.confirm("Post it?", default=True):
            continue
        try:
            with console.status("Posting…"):
                post_url = plat.submit(Reply(platform, r["thread_key"].split(":", 1)[1], r["source"], r["text"]))
        except Exception as e:  # noqa: BLE001 - record any platform failure and continue with the rest
            svc.store.mark_post(r["id"], "failed", error=str(e))
            ui.error(f"Failed: {escape(str(e))}")
            continue
        svc.store.mark_post(r["id"], "posted", url=post_url)
        svc.store.set_status(r["thread_key"], "posted")
        ui.ok(f"Posted · [link={post_url}]{post_url}[/link]")


# ---------------------------------------------------------------- reporting


@app.command(rich_help_panel="Insights")
def stats(
    platform: str = PlatformOpt,
    insights: bool = typer.Option(False, help="Ask Claude what makes your best comments work; "
                                              "the result feeds future drafts."),
):
    """Your activity: likes/karma by community, hour and length."""
    from wingman.core.stats import summarize, write_learnings

    svc = _services()
    plat = svc.platform(platform)
    try:
        with console.status("Fetching your posts…"):
            svc.store.save_activity(plat.my_activity(limit=1000))
    except WingmanError as e:
        ui.warn(f"Couldn't refresh from {platform} ({escape(str(e))}); showing saved data.")
    items = svc.store.activity(platform)
    if not plat.has_scores:
        if not items:
            items = svc.store.logged_posts(platform)
            ui.muted("Showing replies you've posted through wingman.")
        if items:
            views.activity(items)
        else:
            ui.info("No comments found yet.")
        if insights:
            ui.warn("--insights needs vote counts, which this platform mode doesn't provide.")
        return
    summary = summarize(items, svc.store.wingman_post_urls())
    if summary is None:
        ui.info("No comments found yet.")
        return
    views.stats(summary)
    if insights:
        with console.status("Analysing what works…"):
            path = write_learnings(svc.llm, items, svc.cfg.data_dir)
        views.learnings(path.read_text())
        ui.ok(f"Saved to {path}; drafts will use it from now on.")


@app.command(rich_help_panel="Insights")
def cost():
    """What wingman has spent on Claude, by task and model."""
    from wingman.core.usage import summarize

    entries = _services().usage_log.entries()
    if not entries:
        ui.info("No usage recorded yet.")
        return
    views.cost(summarize(entries, datetime.now(UTC).astimezone()))


@app.command("quality", rich_help_panel="Insights")
def quality_report(platform: str = typer.Option(None, "--platform", "-p", help="Only this platform.")):
    """How good the drafts are, from your ratings: by model, by platform, and what tends to be off."""
    from wingman.core import quality

    s = quality.summarize(_services().store.ratings(platform))
    if s is None:
        ui.info("No ratings yet. After each thread in [accent]draft[/], [accent]review[/] or [accent]post[/], "
                "rate the drafts 1–5.")
        return
    views.quality(s)


# ---------------------------------------------------------------- entry point


def main() -> None:
    """Console entry point: user-facing errors print as one line instead of a traceback."""
    logging.basicConfig(level=logging.WARNING, format="%(message)s",
                        handlers=[RichHandler(console=console, show_time=False, show_path=False)])
    try:
        app()
    except NotReadyError as e:
        ui.warn(escape(str(e)))
        raise SystemExit(1) from None
    except WingmanError as e:
        ui.error(escape(str(e)))
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        console.print()
        ui.muted("Stopped.")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
