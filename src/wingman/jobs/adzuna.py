# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Adzuna job search API (https://developer.adzuna.com). Free key; personal job search is permitted.

GET https://api.adzuna.com/v1/api/jobs/{country}/search/{page}?app_id=…&app_key=…&what=…
Descriptions are only the start of each ad, so jobs from here are marked `snippet_only`.
"""

from __future__ import annotations

import os
import urllib.error
from datetime import datetime
from urllib.parse import urlencode

from wingman.config import JobSearch
from wingman.core import http
from wingman.core.models import now_utc
from wingman.errors import ConfigError, WingmanError
from wingman.jobs.models import Job

API = "https://api.adzuna.com/v1/api/jobs/{country}/search/{page}"
PAGE_SIZE = 50  # Adzuna's maximum results_per_page
ATTRIBUTION = "Jobs by Adzuna"


def credentials() -> tuple[str, str]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID", "").strip(), os.environ.get("ADZUNA_APP_KEY", "").strip()
    if not app_id or not app_key:
        missing = " and ".join(n for n, v in (("ADZUNA_APP_ID", app_id), ("ADZUNA_APP_KEY", app_key)) if not v)
        raise ConfigError(f"Set {missing} in .env (free from https://developer.adzuna.com), "
                          "or use `wingman jobs draft <url>` with an ad you copied.")
    return app_id, app_key


def _created(value: str | None) -> datetime:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return now_utc()


def to_job(r: dict) -> Job:
    work = " · ".join(v.replace("_", " ") for v in (r.get("contract_type"), r.get("contract_time")) if v)
    salary_min, salary_max = r.get("salary_min"), r.get("salary_max")
    return Job(
        source="adzuna", id=str(r["id"]), title=(r.get("title") or "").strip(),
        company=((r.get("company") or {}).get("display_name") or "").strip(),
        location=((r.get("location") or {}).get("display_name") or "").strip(),
        description=(r.get("description") or "").strip(), url=r.get("redirect_url") or "",
        created_at=_created(r.get("created")), salary_min=salary_min, salary_max=salary_max,
        salary_predicted=str(r.get("salary_is_predicted", "0")) == "1", work_type=work, snippet_only=True,
    )


class AdzunaClient:
    def __init__(self, app_id: str, app_key: str, country: str = "au", get_json=None):
        self.app_id, self.app_key, self.country = app_id, app_key, country
        self._get_json = get_json or http.get_json

    def _params(self, search: JobSearch, max_days_old: int, page_size: int) -> dict:
        params = {"app_id": self.app_id, "app_key": self.app_key, "what": search.what,
                  "results_per_page": page_size, "max_days_old": max_days_old, "sort_by": "date",
                  "content-type": "application/json"}
        if search.where:
            params["where"] = search.where
        if search.exclude:
            params["what_exclude"] = " ".join(search.exclude)
        if search.salary_min:
            params["salary_min"] = search.salary_min
        if search.full_time:
            params["full_time"] = 1
        if search.permanent:
            params["permanent"] = 1
        return params

    def search(self, search: JobSearch, max_days_old: int, limit: int) -> list[Job]:
        jobs: list[Job] = []
        page = 1
        while len(jobs) < limit:
            size = min(PAGE_SIZE, limit - len(jobs))
            query = urlencode(self._params(search, max_days_old, size))
            url = API.format(country=self.country, page=page) + "?" + query
            try:
                data = self._get_json(url)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    raise WingmanError("Adzuna rejected the credentials: check ADZUNA_APP_ID and "
                                       "ADZUNA_APP_KEY.") from e
                raise WingmanError(f"Adzuna search failed (HTTP {e.code}) for “{search.what}”.") from e
            results = data.get("results") or []
            jobs += [to_job(r) for r in results if r.get("id")]
            if len(results) < size:
                break
            page += 1
        return jobs[:limit]


def search_all(client: AdzunaClient, searches: list[JobSearch], max_days_old: int, limit: int,
               exclude_companies: list[str]) -> list[Job]:
    """Every search, deduplicated by ad id (and by title+company, as agencies repost), minus excluded companies."""
    blocked = {c.strip().lower() for c in exclude_companies if c.strip()}
    seen_ids: set[str] = set()
    seen_roles: set[tuple[str, str]] = set()
    out: list[Job] = []
    for s in searches:
        for job in client.search(s, max_days_old, limit):
            role = (job.title.lower(), job.company.lower())
            if job.id in seen_ids or role in seen_roles:
                continue
            if job.company.lower() in blocked:
                continue
            seen_ids.add(job.id)
            seen_roles.add(role)
            out.append(job)
    return out
