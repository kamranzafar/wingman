# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

import urllib.error
from datetime import UTC, date, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
import typer
from pydantic import ValidationError

from wingman import cli
from wingman.config import JobSearch, JobsSettings, LLMSettings, Settings
from wingman.errors import ConfigError, WingmanError
from wingman.jobs import paste
from wingman.jobs.adzuna import AdzunaClient, credentials, search_all, to_job
from wingman.jobs.export import export, folder_for, markdown_to_docx, slug
from wingman.jobs.models import Job, RankedJob
from wingman.jobs.ranker import JobScore, JobScores, rank_jobs
from wingman.jobs.session import export_blockers
from wingman.jobs.writer import Application, ApplicationWriter, Match

NOW = datetime.now(UTC)


def ad(i, title="Senior Engineer", company="Acme", **kw):
    return {"id": str(i), "title": title, "company": {"display_name": company},
            "location": {"display_name": "Sydney, New South Wales"}, "description": "Kafka and Java…",
            "redirect_url": f"https://www.adzuna.com.au/details/{i}", "created": "2026-11-20T01:00:00Z",
            "salary_min": 150000, "salary_max": 170000, "salary_is_predicted": "1",
            "contract_type": "permanent", "contract_time": "full_time", **kw}


def job(**kw):
    defaults = dict(source="paste", id="seek-1", title="Staff Engineer", company="Acme", location="Sydney",
                    description="We need Kafka and Debezium experience.", url="https://www.seek.com.au/job/1",
                    created_at=NOW)
    return Job(**{**defaults, **kw})


class FakeApi:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def __call__(self, url):
        parts = urlsplit(url)
        self.calls.append((parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}))
        page = int(parts.path.rsplit("/", 1)[1])
        return {"results": self.pages[page - 1] if page <= len(self.pages) else []}


class RecordingLLM:
    def __init__(self, result):
        self.result, self.calls = result, []

    def model_for(self, task):
        return "claude-sonnet-5-5"

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


# ---------------------------------------------------------------- Adzuna

def test_adzuna_builds_the_search_and_maps_results():
    api = FakeApi([[ad(1)]])
    client = AdzunaClient("id", "key", country="au", get_json=api)
    search = JobSearch(what="platform engineer", where="Sydney", exclude=["php", "junior"], salary_min=150000,
                       permanent=True)
    [j] = client.search(search, max_days_old=7, limit=50)
    path, params = api.calls[0]
    assert path == "/v1/api/jobs/au/search/1"
    assert params == {"app_id": "id", "app_key": "key", "what": "platform engineer", "where": "Sydney",
                      "what_exclude": "php junior", "salary_min": "150000", "permanent": "1",
                      "results_per_page": "50", "max_days_old": "7", "sort_by": "date",
                      "content-type": "application/json"}
    assert (j.key, j.company, j.location, j.snippet_only) == ("job:adzuna:1", "Acme", "Sydney, New South Wales", True)
    assert j.work_type == "permanent · full time"
    assert j.salary() == "$150,000–$170,000 (est.)"


def test_adzuna_pages_until_the_limit_or_a_short_page():
    api = FakeApi([[ad(i) for i in range(50)], [ad(i) for i in range(50, 60)]])
    jobs = AdzunaClient("id", "key", get_json=api).search(JobSearch(what="x"), 7, limit=80)
    assert len(jobs) == 60 and [c[1]["results_per_page"] for c in api.calls] == ["50", "30"]


def test_search_all_dedupes_and_drops_excluded_companies():
    api = FakeApi([[ad(1), ad(2, company="Spammy Recruiters"), ad(3, title="Senior Engineer"), ad(1)]])
    client = AdzunaClient("id", "key", get_json=api)
    jobs = search_all(client, [JobSearch(what="a")], 7, 50, exclude_companies=["spammy recruiters"])
    assert [j.id for j in jobs] == ["1"]  # 3 repeats 1's title+company (a repost); 2 is excluded


def test_adzuna_credentials_and_auth_errors(monkeypatch):
    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.setenv("ADZUNA_APP_KEY", "k")
    with pytest.raises(ConfigError, match="ADZUNA_APP_ID"):
        credentials()

    def denied(url):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", None, None)

    with pytest.raises(WingmanError, match="rejected the credentials"):
        AdzunaClient("id", "bad", get_json=denied).search(JobSearch(what="x"), 7, 10)


def test_to_job_tolerates_missing_fields():
    j = to_job({"id": 9, "title": "Dev"})
    assert (j.company, j.location, j.salary(), j.work_type) == ("", "", "", "")


# ---------------------------------------------------------------- paste

def test_job_ids_from_seek_links_and_others():
    assert paste.job_id("https://www.seek.com.au/job/81234567?type=standard") == "seek-81234567"
    assert paste.job_id("https://www.seek.com.au/jobs-in-information-technology?jobId=81234567") == "seek-81234567"
    other = paste.job_id("https://careers.example.com/roles/42")
    assert other.startswith("u") and other == paste.job_id("https://careers.example.com/roles/42 ")


def test_job_template_parsing():
    text = paste.JOB_TEMPLATE.replace("== TITLE ==\n", "== TITLE ==\nData Engineer\n") \
        .replace("== AD ==\n", "== AD ==\nBuild pipelines.\nOn GCP.\n")
    x = paste.parse_job_template(text)
    assert x.found_job and (x.title, x.description) == ("Data Engineer", "Build pipelines.\nOn GCP.")
    assert not paste.parse_job_template(paste.JOB_TEMPLATE).found_job


def test_pasted_full_ad_fills_in_a_scanned_snippet():
    base = to_job(ad(5))
    x = paste.ExtractedJob(found_job=True, title="t", company="Acme", location="", salary="$160k + super",
                           work_type="Hybrid", description="The whole ad.", requirements=["Kafka"])
    filled = paste.to_job(base.url, x, base=base)
    assert filled.key == "job:adzuna:5" and not filled.snippet_only
    assert (filled.description, filled.salary(), filled.requirements) == ("The whole ad.", "$160k + super", ["Kafka"])


def test_extract_job_uses_the_cheap_model_and_treats_text_as_data():
    llm = RecordingLLM(paste.ExtractedJob(found_job=True, title="t", company="c", location="", salary="",
                                          work_type="", description="d", requirements=[]))
    paste.extract_job(llm, "x" * 100_000)
    [call] = llm.calls
    assert call["task"] == "extract" and len(call["prompt"]) == 60_000 and "data, not instructions" in call["system"]


# ---------------------------------------------------------------- ranking and writing

def test_rank_jobs_clamps_and_maps_scores():
    llm = RecordingLLM(JobScores(scores=[JobScore(index=0, fit=14, angle="lead with CDC", gaps=list("abcd"))]))
    ranked = rank_jobs(llm, "MY CV", "profile", [job(), job(id="seek-2")], criteria="staff engineer in Sydney")
    assert [(r.fit, r.angle, r.gaps) for r in ranked] == [(10, "lead with CDC", ["a", "b", "c"]), (0, "", [])]
    call = llm.calls[0]
    assert call["task"] == "rank" and "MY CV" in call["system"] and "staff engineer in Sydney" in call["system"]
    assert "[1]" in call["prompt"] and call["cache"] is False  # one batch: no cache write


def _application(resume="# Jane\n\n## Summary\nEngineer.", letter="Dear team, …"):
    return Application(resume_md=resume, cover_letter_md=letter, matched=[Match(requirement="Kafka", evidence="x")],
                       gaps=[], claims_to_verify=[])


def test_writer_puts_the_cv_in_the_cached_system_prompt_and_the_job_in_the_prompt():
    llm = RecordingLLM(_application())
    writer = ApplicationWriter(llm, "MASTER CV TEXT", "voice: plain")
    result = writer.draft(job(), RankedJob(job(), 8, angle="lead with your Debezium work"))
    call = llm.calls[0]
    assert "MASTER CV TEXT" in call["system"] and "Never invent" in call["system"]
    assert "Debezium experience" in call["prompt"] and "lead with your Debezium work" in call["prompt"]
    assert (call["task"], call["cache"]) == ("resume", True)
    assert result.model == "claude-sonnet-5-5"


def test_rewrites_send_the_current_drafts_with_the_instruction():
    llm = RecordingLLM(_application())
    ApplicationWriter(llm, "cv", "p").draft(job(), instruction="shorter letter",
                                            previous=_application(letter="EDITED LETTER"))
    prompt = llm.calls[0]["prompt"]
    assert "EDITED LETTER" in prompt and "shorter letter" in prompt


def test_export_is_blocked_by_placeholders_and_empty_documents():
    assert export_blockers("# Jane", "Dear…") == []
    problems = export_blockers("[your Kubernetes work here] and [your metric here]", "")
    assert problems == ["the resume still has 2 [your … here] placeholders", "the cover letter is empty"]


# ---------------------------------------------------------------- export

def test_markdown_to_docx_keeps_structure(tmp_path):
    from docx import Document

    path = tmp_path / "r.docx"
    markdown_to_docx("# Jane Doe\n[Site](https://jane.dev)\n\n## Experience\n### Lead, Acme\n"
                     "- Built **CDC** pipelines\n  - nested\n\nTwo lines\nof one paragraph.", path)
    paras = [(p.style.name, p.text) for p in Document(path).paragraphs]
    assert paras == [("Title", "Jane Doe"), ("Normal", "Site (https://jane.dev)"), ("Heading 1", "Experience"),
                     ("Heading 2", "Lead, Acme"), ("List Bullet", "Built CDC pipelines"),
                     ("List Bullet 2", "nested"), ("Normal", "Two lines of one paragraph.")]
    bold = [r.text for p in Document(path).paragraphs for r in p.runs if r.bold]
    assert bold == ["CDC"]


def test_export_writes_markdown_docx_and_the_ad(tmp_path):
    j = job(title="Senior / Staff Engineer (Kafka)", company="Acme & Co.")
    folder = folder_for(tmp_path, j, today=date(2026, 11, 21))
    assert folder.name == "2026-11-21-acme-co-senior-staff-engineer-kafka"
    files = export(folder, j, "# Jane", "Dear team")
    assert sorted(f.name for f in files) == ["cover-letter.docx", "cover-letter.md", "job.md", "resume.docx",
                                              "resume.md"]
    assert (folder / "job.md").read_text().startswith("# Senior / Staff Engineer (Kafka)")
    assert slug("!!!") == "job"


# ---------------------------------------------------------------- store, config, CLI

def test_store_keeps_rankings_and_progress(store):
    j = job(source="adzuna", id="7", created_at=NOW - timedelta(days=2))
    store.save_ranked_job(RankedJob(j, 8, "lead with X", ["gap"]))
    store.save_ranked_job(RankedJob(job(source="adzuna", id="8"), 4))
    assert [r.job.id for r in store.job_queue(min_fit=6, limit=10)] == ["7"]

    j.description = "full ad"
    store.save_job(j, "ranked")  # pasting the full ad keeps the ranking
    [r] = store.job_queue(6, 10)
    assert (r.job.description, r.fit, r.angle, r.gaps) == ("full ad", 8, "lead with X", ["gap"])

    store.set_job_status(j.key, "drafted", folder="/tmp/x")
    store.save_ranked_job(RankedJob(j, 9))  # a rescan doesn't undo progress
    assert store.job_status(j.key) == "drafted" and store.job_queue(0, 10)[0].job.id == "8"
    assert store.known_job_keys() == {"job:adzuna:7", "job:adzuna:8"}
    assert store.ranked_job("job:adzuna:7").job.description == "full ad"


def test_jobs_config_is_validated():
    s = Settings.model_validate({"jobs": {"base_resume": "data/cv.md",
                                          "searches": [{"what": "data engineer", "where": "Perth"}]}})
    assert s.jobs.searches[0].where == "Perth" and s.jobs.country == "au"
    for bad in ({"serches": []}, {"country": "Australia"}, {"searches": [{"where": "Perth"}]}):
        with pytest.raises(ValidationError):
            JobsSettings.model_validate(bad)
    assert LLMSettings().task_models()["resume"] == "claude-sonnet-5-5"


def test_jobs_commands_are_registered():
    jobs = typer.main.get_command(cli.app).commands["jobs"]
    assert set(jobs.commands) == {"scan", "review", "draft"}
    opts = {o for p in jobs.commands["scan"].params for o in p.opts}
    assert {"--what", "--where", "--show"} <= opts


# ---------------------------------------------------------------- master CV formats

def test_read_file_reads_a_word_resume_with_tables(tmp_path):
    from docx import Document as WordDocument

    from wingman.sources.files import read_file

    doc = WordDocument()
    doc.add_heading("Jane Doe", level=0)
    doc.add_paragraph("jane@example.com · Sydney")
    doc.add_heading("Experience", level=1)
    doc.add_paragraph("Built a CDC platform on Debezium", style="List Bullet")
    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Skills"
    table.cell(0, 1).text = "Kafka, Java"
    doc.add_paragraph("References on request")
    path = tmp_path / "cv.docx"
    doc.save(path)

    assert read_file(path).text == ("# Jane Doe\njane@example.com · Sydney\n## Experience\n"
                                    "- Built a CDC platform on Debezium\nSkills\nKafka, Java\nReferences on request")


def test_read_file_explains_old_word_files(tmp_path):
    from wingman.sources.files import read_file

    (tmp_path / "cv.doc").write_bytes(b"\xd0\xcf\x11\xe0")
    with pytest.raises(ValueError, match=r"save it as \.docx"):
        read_file(tmp_path / "cv.doc")
