from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from tests.conftest import fake_response
from wingman.core.drafter import Draft, Drafter, DraftingOptions, DraftSet
from wingman.core.llm import LLM
from wingman.core.models import Constraints, Document, RankedThread
from wingman.core.retrieval import Index
from wingman.core.usage import UsageLog, estimate_cost, short_model_name, summarize
from wingman.errors import LLMError


class Answer(BaseModel):
    x: int


def _llm(client, tmp_path=None, **kw):
    log = UsageLog(tmp_path / "usage.jsonl") if tmp_path else None
    return LLM("claude-opus-5-5", "medium", task_models={"draft": "claude-sonnet-5-5", "extract": "claude-haiku-5-5"},
               usage_log=log, client=client, **kw)


def test_task_routing_and_fallbacks_only_where_supported(fake_client):
    client, calls, set_response = fake_client
    set_response(fake_response(Answer(x=1)))
    llm = _llm(client)
    for task in ("draft", "extract", None):
        llm.parse(system="s", prompt="p", schema=Answer, task=task)
    assert [c["model"] for c in calls] == ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5"]
    assert [c.get("fallbacks") for c in calls] == ["default", None, "default"]  # Haiku has no fallbacks
    assert calls[0]["output_config"] == {"effort": "medium"}


def test_usage_is_logged_even_when_the_result_is_unusable(fake_client, tmp_path):
    client, _, set_response = fake_client
    set_response(fake_response(None, stop_reason="refusal", input_tokens=2000, output_tokens=100))
    llm = _llm(client, tmp_path)
    with pytest.raises(LLMError, match="declined"):
        llm.parse(system="s", prompt="p", schema=Answer, task="draft")
    [entry] = llm.usage_log.entries()
    assert entry.task == "draft" and entry.cost == pytest.approx(estimate_cost("claude-sonnet-5-5", 2000, 100))


def test_cost_estimates_and_summary():
    assert estimate_cost("claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(2.0 + 1.0)
    assert estimate_cost("claude-haiku-5-5", 6000, 1200) == pytest.approx(0.0012)
    assert short_model_name("claude-sonnet-5-5") == "Sonnet 5.5"
    from wingman.core.usage import UsageEntry
    now = datetime(2026, 11, 18, 12, tzinfo=UTC)
    entries = [UsageEntry("2026-11-18T09:00:00+00:00", "draft", "claude-sonnet-5-5", 5000, 0, 0, 2000, 0.03),
               UsageEntry("2026-10-01T09:00:00+00:00", "profile", "claude-sonnet-5-5", 38000, 0, 0, 7000, 0.146)]
    s = summarize(entries, now)
    assert (s.today, s.month, round(s.all_time, 3), s.requests) == (0.03, 0.03, 0.176, 2)
    assert s.by_task[0].task == "profile"  # most expensive first


class RecordingLLM:
    def __init__(self):
        self.kwargs = None

    def model_for(self, task):
        return "claude-haiku-5-5"

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return DraftSet(should_reply=True, reason="", rule_concerns=[], drafts=[Draft(angle="a", text="t")],
                        claims_to_verify=[])


def test_drafter_redacts_private_terms_everywhere_and_reports_sources(thread):
    idx = Index.build([Document("CDC with Debezium", "At ACME we tuned Debezium snapshot.mode for restarts. " * 3,
                                "blog", date="2025-07-03")], embedder=None)
    llm = RecordingLLM()
    options = DraftingOptions(num_drafts=2, excerpts=4, private_terms=("ACME",))
    drafter = Drafter(llm, "Summary: engineer at ACME.", Constraints(max_length=10_000), options, index=idx)
    result = drafter.draft(thread, RankedThread(thread, 8, 0.5, 0.7, "explain snapshot.mode"), "shorter")

    sent = llm.kwargs["system"] + llm.kwargs["prompt"]
    assert "ACME" not in sent
    assert sent.count("[employer]") == 4  # once in the profile + the excerpt's sentence repeated 3 times
    assert "Give exactly 2 drafts" in llm.kwargs["system"]
    assert "fenced code blocks" in llm.kwargs["system"] and "Never refer to another draft" in llm.kwargs["system"]
    assert "explain snapshot.mode" in llm.kwargs["prompt"] and "shorter" in llm.kwargs["prompt"]
    assert result.sources == ["CDC with Debezium"] and llm.kwargs["task"] == "draft"
    assert result.model == "claude-haiku-5-5"


def test_system_prompt_is_cached_only_when_asked(fake_client):
    client, calls, set_response = fake_client
    set_response(fake_response(Answer(x=1)))
    llm = _llm(client)
    llm.parse(system="s", prompt="p", schema=Answer)
    llm.parse(system="s", prompt="p", schema=Answer, cache=True)
    assert "cache_control" not in calls[0]["system"][0]
    assert calls[1]["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_drafter_writes_to_the_length_limit_and_caches_only_in_batch(thread):
    llm = RecordingLLM()
    Drafter(llm, "Summary.", Constraints(max_length=10_000), DraftingOptions(max_chars=1500)).draft(thread)
    assert "about 1275 characters; never more than 1500" in llm.kwargs["system"] and llm.kwargs["cache"] is False
    batch = DraftingOptions(max_chars=1500, cache_prompt=True)
    Drafter(llm, "Summary.", Constraints(max_length=500), batch).draft(thread)
    assert "never more than 500" in llm.kwargs["system"] and llm.kwargs["cache"] is True  # platform limit wins


class ShorteningLLM(RecordingLLM):
    """Returns over-long drafts first, then (for the tightening call) shorter versions."""

    def __init__(self, first: list[str], shortened: list[str]):
        super().__init__()
        self.first, self.shortened, self.calls = first, shortened, []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        from wingman.core.drafter import Shortened
        if kwargs["schema"] is Shortened:
            return Shortened(texts=self.shortened)
        return DraftSet(should_reply=True, reason="", rule_concerns=[], claims_to_verify=[],
                        drafts=[Draft(angle=f"a{i}", text=t) for i, t in enumerate(self.first)])


def test_only_over_limit_drafts_are_shortened(thread):
    llm = ShorteningLLM(first=["ok " * 20, "x" * 340], shortened=["y" * 250])
    result = Drafter(llm, "Summary.", Constraints(max_length=300), DraftingOptions(max_chars=1500)).draft(thread)
    assert [len(d.text) for d in result.drafts.drafts] == [60, 250]
    shorten = llm.calls[1]
    assert "Hard limit: 300" in shorten["prompt"] and "Currently 340 characters of words" in shorten["prompt"]
    assert "at most 255 characters (about 42 words), i.e. cut about 85 characters" in shorten["prompt"]
    assert "ok ok" not in shorten["prompt"]  # the draft that already fit isn't sent again


def test_no_shortening_call_when_everything_fits(thread):
    llm = ShorteningLLM(first=["short reply"], shortened=[])
    Drafter(llm, "Summary.", Constraints(max_length=300), DraftingOptions()).draft(thread)
    assert len(llm.calls) == 1


def test_a_longer_rewrite_is_never_used(thread):
    llm = ShorteningLLM(first=["x" * 340], shortened=["z" * 400])
    result = Drafter(llm, "Summary.", Constraints(max_length=300), DraftingOptions()).draft(thread)
    assert result.drafts.drafts[0].text == "x" * 340  # still flagged in review, but not made worse



def test_links_are_taken_out_of_the_word_budget():
    from wingman.core.drafter import _shorten_brief, prose_budget
    url = "https://example.com/blog/2026/10/30/feature-toggles-in-java-without-the-if-else/"
    text = "w" * 230 + " " + url
    assert prose_budget(300, text) == 300 - len(url) - 1
    brief = _shorten_brief(text, 300, len)
    words_chars = int((300 - len(url) - 1) * 0.85)
    assert f"at most {words_chars} characters (about {words_chars // 6} words)" in brief
    assert url in brief and "isn't part of that budget" in brief


def test_a_second_pass_runs_if_the_first_is_still_too_long(thread):
    class TwoStep(ShorteningLLM):
        def parse(self, **kwargs):
            from wingman.core.drafter import Shortened
            if kwargs["schema"] is Shortened:
                self.calls.append(kwargs)
                return Shortened(texts=["y" * (310 if len(self.calls) == 2 else 280)])
            return super().parse(**kwargs)

    llm = TwoStep(first=["x" * 340], shortened=[])
    result = Drafter(llm, "Summary.", Constraints(max_length=300), DraftingOptions()).draft(thread)
    assert len(result.drafts.drafts[0].text) == 280 and len(llm.calls) == 3  # draft + 2 shortening passes
