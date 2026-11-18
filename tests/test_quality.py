import pytest

from wingman.config import PacingSettings
from wingman.core import quality
from wingman.core.drafter import Draft, DraftResult, DraftSet
from wingman.core.quality import Rating, parse_issues, similarity, suggested_rating, summarize
from wingman.core.review import ReviewSession, Selection
from wingman.platforms.reddit.paste import RedditPastePlatform


@pytest.mark.parametrize(("sim", "rating"), [(1.0, 5), (0.97, 5), (0.9, 4), (0.6, 3), (0.2, 2)])
def test_suggested_rating_follows_how_much_you_edited(sim, rating):
    assert suggested_rating(sim) == rating


def test_similarity():
    assert similarity("same text", "same text") == 1.0
    assert similarity("Use a router per flag.", "Use one router per flag, always.") > 0.7
    assert similarity("Use a router per flag.", "Totally different reply.") < 0.5


@pytest.mark.parametrize(("answer", "issues"), [
    ("fv", ["facts", "voice"]), ("f v", ["facts", "voice"]), ("facts, length", ["facts", "length"]),
    ("xz", []), ("", []), ("ff", ["facts"]),
])
def test_parse_issues(answer, issues):
    assert parse_issues(answer) == issues


def test_summary_by_model_and_issues():
    ratings = [
        Rating("bluesky", "k1", "debezium", "claude-haiku-5-5", 4, "posted", 0.9),
        Rating("bluesky", "k2", "feature flags", "claude-haiku-5-5", 2, "skipped", issues=["facts", "voice"]),
        Rating("reddit", "k3", "java", "claude-sonnet-5-5", 5, "posted", 1.0, regenerations=1),
        Rating("reddit", "k4", "java", "claude-haiku-5-5", 3, "posted", 0.6, issues=["facts"]),
    ]
    s = summarize(ratings)
    assert s.count == 4 and s.avg_rating == 3.5 and s.posted_pct == 75
    assert s.distribution == {5: 1, 4: 1, 3: 1, 2: 1, 1: 0}
    assert s.by_model[0].name == "claude-haiku-5-5" and s.by_model[0].count == 3
    assert s.issues[0] == ("facts", 2)
    assert summarize([]) is None


def _selection(draft_text: str, own_text: str | None = None) -> Selection:
    result = DraftResult(DraftSet(should_reply=True, reason="", rule_concerns=[],
                                  drafts=[Draft(angle="a", text=draft_text)], claims_to_verify=[]),
                         model="claude-haiku-5-5")
    return Selection(result, 0, own_text)


@pytest.fixture
def session(store):
    return ReviewSession(RedditPastePlatform({}), store, drafter=None, pacing=PacingSettings(), private_terms=[])


def test_posting_unchanged_suggests_5_and_enter_accepts(session, thread, monkeypatch):
    answers = iter([""])  # just Enter
    monkeypatch.setattr("wingman.core.review.Prompt.ask", lambda *a, default="", **k: next(answers) or default)
    session._rate(thread, _selection("Use a router per flag."), "posted")
    [r] = session.store.ratings()
    assert (r.rating, r.similarity, r.model, r.outcome) == (5, 1.0, "claude-haiku-5-5", "posted")


def test_low_rating_asks_what_was_off(session, thread, monkeypatch):
    answers = iter(["2", "fv"])
    monkeypatch.setattr("wingman.core.review.Prompt.ask", lambda *a, **k: next(answers))
    sel = _selection("draft", own_text="something I wrote myself")
    sel.regenerations = 2
    session._rate(thread, sel, "posted")
    [r] = session.store.ratings()
    assert r.rating == 2 and r.issues == ["facts", "voice"] and r.regenerations == 2 and r.similarity < 0.5


def test_skipping_the_rating_records_nothing(session, thread, monkeypatch):
    monkeypatch.setattr("wingman.core.review.Prompt.ask", lambda *a, **k: "")
    session._rate(thread, _selection("draft"), "skipped")  # no default when nothing was posted
    assert session.store.ratings() == []


def test_no_rating_without_drafts_or_when_disabled(session, thread, monkeypatch):
    monkeypatch.setattr("wingman.core.review.Prompt.ask", lambda *a, **k: pytest.fail("should not ask"))
    session._rate(thread, Selection(), "posted")  # no drafts (e.g. you wrote it yourself)
    session.ask_rating = False
    session._rate(thread, _selection("draft"), "posted")
    assert quality.summarize(session.store.ratings()) is None


def test_back_returns_to_previous_thread_without_redrafting(store, thread, monkeypatch):
    from dataclasses import replace

    from wingman.core.models import RankedThread

    class CountingDrafter:
        def __init__(self):
            self.calls: list[str] = []

        def draft(self, t, ranking=None, instruction=""):
            self.calls.append(t.key)
            return _selection(f"draft for {t.id}").result

    class CountingPlatform(RedditPastePlatform):
        def __init__(self, settings):
            super().__init__(settings)
            self.fetched: list[str] = []

        def fetch_context(self, t):
            self.fetched.append(t.key)
            return t

    a, b = (RankedThread(replace(thread, id=i), 8, 0.5, 0.7) for i in ("A", "B"))
    plat, drafter = CountingPlatform({}), CountingDrafter()
    answers = iter(["b", "n",   # A: back is refused on the first thread, then next
                    "b",        # B: back to A
                    "n",        # A again: same drafts, next
                    "q", ""])   # B again: quit, skip the rating
    monkeypatch.setattr("wingman.core.review.Prompt.ask", lambda *args, **kw: next(answers))
    ReviewSession(plat, store, drafter, PacingSettings(), []).run([a, b])
    assert drafter.calls == ["reddit:A", "reddit:B"]  # each thread drafted once despite revisits
    assert plat.fetched == ["reddit:A", "reddit:B"]
    assert next(answers, "exhausted") == "exhausted"


def test_cannot_post_the_same_thread_twice_in_a_session(store, thread, monkeypatch):
    session = ReviewSession(RedditPastePlatform({}), store, None, PacingSettings(), [])
    monkeypatch.setattr("wingman.core.review.clipboard.copy", lambda t: True)
    monkeypatch.setattr("wingman.core.review.webbrowser.open", lambda u: None)
    monkeypatch.setattr("wingman.core.review.Prompt.ask", lambda *a, **k: "")
    store.save_pasted(thread)
    assert session._post(thread, "first reply") == "posted"
    assert session._post(thread, "second reply") is None
    assert len(store.logged_posts("reddit")) == 1
