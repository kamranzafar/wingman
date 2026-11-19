# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

from datetime import UTC, datetime

from wingman import views
from wingman.core.models import ActivityItem
from wingman.core.stats import activity_breakdowns, summarize


def _item(n: int, source: str, score: int, body: str = "x") -> ActivityItem:
    return ActivityItem("reddit", str(n), source, body, score, datetime(2026, 1, 1, 12, tzinfo=UTC), f"u/{n}")


def test_summarize_empty():
    assert summarize([], set()) is None


def test_summarize_groups_and_wingman():
    items = [_item(1, "python", 10), _item(2, "python", 2, "y" * 150), _item(3, "rust", 30)]
    s = summarize(items, {"u/3"})
    assert (s.count, s.total, s.median) == (3, 42, 10)
    by_source = next(b for b in s.breakdowns if b.title == "By community")
    assert [r[0] for r in by_source.rows] == ["rust", "python"]  # sorted by average score
    by_len = next(b for b in s.breakdowns if b.title == "By length")
    assert [r[0] for r in by_len.rows] == ["< 100 chars", "100-300"]
    assert s.wingman == (1, 30)
    assert s.top[0].id == "3"
    views.stats(s)  # renders without error


def test_activity_breakdowns_counts():
    items = [_item(1, "a", 0), _item(2, "a", 0), _item(3, "b", 0)]
    by_source = activity_breakdowns(items)[0]
    assert by_source.rows == [("a", 2), ("b", 1)]
    views.activity(items)


def test_snippet_cuts_on_word_boundary_and_strips_markdown():
    from wingman.views import _snippet
    assert _snippet("**bold**  text\n\nmore", 100) == "bold text more"
    assert _snippet("alpha beta gamma delta", 13) == "alpha beta…"


def _ranked(n: int):
    from datetime import timedelta

    from wingman.core.models import RankedThread, Thread, now_utc
    t = Thread(platform="bluesky", id=str(n), source="pgvector", title=f"post {n}", body=f"post {n} body",
               url="https://bsky.app/x", author="a", score=1, num_comments=0, created_at=now_utc() - timedelta(hours=1))
    return RankedThread(t, 5, 0.5, 0.9 - n / 100, f"angle {n}")


def test_scan_results_pages_forward_and_back(monkeypatch):
    from wingman import ui
    presses = iter("npnnq")
    seen = []
    monkeypatch.setattr(ui, "interactive", lambda: True)
    monkeypatch.setattr(ui, "key", lambda *a, **k: next(presses))
    monkeypatch.setattr(views.console, "clear", lambda: None)
    real_heading = ui.heading
    monkeypatch.setattr(ui, "heading", lambda title, sub="": (seen.append(sub), real_heading(title, sub)))
    views.scan_results(5, 5, [_ranked(i) for i in range(5)], show=5, page_size=2)
    pages = [s.split(" of")[0] for s in seen]
    assert pages == ["page 1", "page 2", "page 1", "page 2", "page 3"]  # n, p, n, n, then q


def test_scan_results_prints_everything_when_not_interactive():
    views.scan_results(5, 5, [_ranked(i) for i in range(5)], show=5, page_size=2)  # no prompts, no hang
