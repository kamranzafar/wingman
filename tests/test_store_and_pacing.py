from datetime import timedelta

from wingman.config import PacingSettings
from wingman.core.models import RankedThread, now_utc
from wingman.core.pacing import next_allowed


def test_redrafting_a_posted_thread_keeps_it_posted(store, thread):
    store.save_pasted(thread)
    store.set_status(thread.key, "posted")
    store.save_pasted(thread)  # `wingman draft` on the same URL again
    assert store.thread_status(thread.key) == "posted"


def test_review_queue_returns_ranked_threads_above_threshold(store, thread):
    thread.created_at = now_utc() - timedelta(hours=1)
    store.save_ranked(RankedThread(thread, relevance=8, opportunity=0.5, final=0.71, angle="snapshot.mode"))
    queue = store.review_queue("reddit", min_final=0.5, max_age_hours=12, limit=10)
    assert len(queue) == 1 and queue[0].angle == "snapshot.mode" and queue[0].thread.top_comments
    assert store.review_queue("reddit", min_final=0.9, max_age_hours=12, limit=10) == []
    assert store.review_queue("reddit", min_final=0.5, max_age_hours=0.5, limit=10) == []


def test_pasted_threads_stay_out_of_the_review_queue(store, thread):
    store.save_pasted(thread)
    assert store.review_queue("reddit", 0, 10**6, 10) == []


def test_logged_posts_become_activity(store, thread):
    store.record_post("reddit", thread.source, thread.key, "my reply", "posted", url="https://r/c/1")
    store.record_post("reddit", thread.source, thread.key, "draft I abandoned", "failed")
    [item] = store.logged_posts("reddit")
    assert item.body == "my reply" and item.permalink == "https://r/c/1"


RULES = PacingSettings(max_per_hour=2, max_per_day=12, max_per_source_per_day=3, min_gap_minutes=10)


def test_pacing_allows_the_first_post(store):
    assert next_allowed(store, "reddit", "java", RULES) is None


def test_pacing_enforces_minimum_gap(store, thread):
    store.record_post("reddit", "java", thread.key, "x", "posted")
    wait = next_allowed(store, "reddit", "java", RULES)
    assert wait is not None and timedelta(minutes=9) < wait - now_utc() <= timedelta(minutes=10)


def test_pacing_enforces_hourly_limit_after_the_gap(store, thread):
    for _ in range(2):
        store.record_post("reddit", "java", thread.key, "x", "posted")
    later = now_utc() + timedelta(minutes=30)  # past the 10-minute gap, still inside the hour
    wait = next_allowed(store, "reddit", "java", RULES, now=later)
    assert wait is not None and wait > later


def test_known_keys_acted_on_only_excludes_still_ranked_threads(store, thread):
    store.save_ranked(RankedThread(thread, relevance=8, opportunity=0.5, final=0.71, angle="a"))
    assert store.known_keys(thread.platform) == {thread.key}
    assert store.known_keys(thread.platform, acted_on_only=True) == set()  # rerank may score it again
    store.set_status(thread.key, "skipped")
    assert store.known_keys(thread.platform, acted_on_only=True) == {thread.key}  # but never resurrect a skip
