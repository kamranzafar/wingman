# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""`wingman jobs …`: scan Adzuna, review matches, or draft for an ad you copied (SEEK or any job site)."""

from __future__ import annotations

import typer
from rich.markup import escape

from wingman import ui, views
from wingman.config import JobSearch, JobsSettings
from wingman.errors import ConfigError, NotReadyError, WingmanError
from wingman.ui import console

jobs_app = typer.Typer(help="Find jobs and draft a tailored resume and cover letter for each. You apply yourself.",
                       no_args_is_help=True)


def _services():
    from wingman.cli import _services as services  # lazy: cli imports this module

    return services()


def _master_cv(svc) -> str:
    from wingman.sources.files import read_file

    name = svc.settings.jobs.base_resume
    if not name:
        raise ConfigError("Set base_resume under [jobs] in wingman.toml to your master CV (.docx, .md, .txt or .pdf). "
                          "It's the only source of facts for your applications.")
    path = svc.cfg.resolve(name)
    try:
        return read_file(path).text
    except FileNotFoundError:
        raise ConfigError(f"Master CV not found: {path} (base_resume under [jobs] in wingman.toml).") from None
    except ValueError as e:
        raise ConfigError(str(e)) from None


def _profile_brief(svc) -> str:
    """Your voice and interests help; they're optional for job applications."""
    try:
        return svc.profile().brief()
    except NotReadyError:
        return "(no profile built yet)"


def _criteria(searches: list[JobSearch]) -> str:
    parts = []
    for s in searches:
        bits = [s.what] + ([f"in {s.where}"] if s.where else []) + ([f"from ${s.salary_min:,}"] if s.salary_min else [])
        parts.append(" ".join(bits))
    return "; ".join(parts)


def _writer(svc, cv: str):
    from wingman.jobs.writer import ApplicationWriter

    return ApplicationWriter(svc.llm, cv, _profile_brief(svc), index=svc.index(),
                             excerpts=min(svc.settings.drafting.excerpts, 3))


def _session(svc, cv: str):
    from wingman.jobs.session import JobReview

    return JobReview(svc.store, _writer(svc, cv), svc.llm, svc.cfg.resolve(svc.settings.jobs.output_dir))


@jobs_app.command()
def scan(
    what: str = typer.Option(None, "--what", "-w", help="Search these keywords instead of your [jobs] searches."),
    where: str = typer.Option("", "--where", help="Location for --what (city, region or postcode)."),
    show: int = typer.Option(20, help="How many top matches to list."),
):
    """Search Adzuna for new jobs and rank them against your master CV.

    Examples: [accent]wingman jobs scan[/] (your saved searches),
    [accent]wingman jobs scan -w "platform engineer" --where Melbourne[/]
    """
    from wingman.jobs.adzuna import ATTRIBUTION, AdzunaClient, credentials, search_all
    from wingman.jobs.ranker import rank_jobs

    svc = _services()
    cfg: JobsSettings = svc.settings.jobs
    searches = [JobSearch(what=what, where=where)] if what else cfg.searches
    if not searches:
        raise ConfigError("Nothing to search: pass --what, or add [[jobs.searches]] to wingman.toml.")
    cv = _master_cv(svc)
    client = AdzunaClient(*credentials(), country=cfg.country)
    plural = "es" if len(searches) > 1 else ""
    ui.heading("Scanning jobs", f"{len(searches)} search{plural} · last {cfg.max_days_old} days")
    with console.status("Searching Adzuna…"):
        jobs = search_all(client, searches, cfg.max_days_old, cfg.per_search_limit, cfg.exclude_companies)
    known = svc.store.known_job_keys()
    fresh = [j for j in jobs if j.key not in known]
    ranked = []
    if fresh:
        with console.status(f"Ranking {len(fresh)} jobs against your CV…"):
            ranked = rank_jobs(svc.llm, cv, _profile_brief(svc), fresh, _criteria(searches))
        for r in ranked:
            svc.store.save_ranked_job(r)
    views.job_results(len(jobs), len(fresh), ranked, show, ATTRIBUTION)


@jobs_app.command()
def review(
    limit: int = typer.Option(10, help="Max jobs to review this session."),
    min_fit: int = typer.Option(None, "--min-fit", min=0, max=10, show_default=False,
                                help="Only jobs with at least this fit, 0-10 (default: min_fit in [jobs])."),
):
    """Go through your best-matching jobs: draft, edit and export a resume and cover letter for each."""
    svc = _services()
    cv = _master_cv(svc)
    threshold = svc.settings.jobs.min_fit if min_fit is None else min_fit
    items = svc.store.job_queue(threshold, limit, max_age_days=30)
    _session(svc, cv).run(items)


@jobs_app.command()
def draft(
    url: str = typer.Argument(None, help="The job ad's URL (prompted if omitted)."),
    editor: bool = typer.Option(False, "--editor", help="Paste the ad into your editor instead of reading the "
                                                        "clipboard."),
):
    """Draft a resume and cover letter for a job ad you found yourself (SEEK or any job site).

    On the ad page, select all and copy first, then run [accent]wingman jobs draft <url>[/].
    Nothing contacts the job site.
    """
    from wingman.core import clipboard
    from wingman.jobs import paste
    from wingman.jobs.models import RankedJob
    from wingman.jobs.ranker import rank_jobs

    url = (url or ui.ask("Job ad URL")).strip()
    svc = _services()
    cv = _master_cv(svc)

    extracted = None
    clip = None if editor else clipboard.paste()
    if clip and len(clip.strip()) >= 200 and clip.strip() != url:
        preview = escape(" ".join(clip.split())[:70])
        if ui.confirm(f"Use the text you copied ({len(clip):,} chars: “{preview}…”)?", default=True):
            with console.status("Reading the ad from your clipboard…"):
                extracted = paste.extract_job(svc.llm, clip)
            if not extracted.found_job:
                ui.warn("That doesn't look like a job ad. Paste it into the editor instead.")
                extracted = None
    if extracted is None:
        extracted = paste.parse_job_template(ui.edit(paste.JOB_TEMPLATE) or "")
        if not extracted.found_job:
            raise WingmanError("No job ad provided (it needs a title and the ad text).")

    job = paste.to_job(url, extracted)
    if svc.store.job_status(job.key) in ("drafted", "applied") and \
            not ui.confirm("You've already drafted for this job. Draft again?"):
        raise typer.Exit()
    with console.status("Checking the fit…"):
        [ranked] = rank_jobs(svc.llm, cv, _profile_brief(svc), [job]) or [RankedJob(job, 0)]
    svc.store.save_job(job, "pasted", ranked.fit, ranked.angle, ranked.gaps)
    ui.heading("Job", job.company)
    _session(svc, cv).review(ranked, single=True)
