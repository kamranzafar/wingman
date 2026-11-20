# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from wingman.config import PacingSettings
from wingman.core.composer import compose_target
from wingman.core.models import LinkCard
from wingman.core.review import ReviewSession
from wingman.errors import WingmanError
from wingman.platforms import get_platform, platform_for_url
from wingman.platforms.hackernews.api import HackerNewsPlatform, parse_source

NOW = datetime.now(UTC)


def hit(sid, author="alice", hours_ago=2, points=5, url="https://example.com/post", text=None, title="Postgres 19"):
    return {"objectID": sid, "title": title, "url": url, "story_text": text, "author": author, "points": points,
            "num_comments": 4, "created_at_i": int((NOW - timedelta(hours=hours_ago)).timestamp())}


class FakeApi:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url):
        parts = urlsplit(url)
        path = parts.path.removeprefix("/api/v1/")
        params = {k: v[0] for k, v in parse_qs(parts.query).items()}
        self.calls.append((path, params))
        route = self.routes[path]
        return route(params) if callable(route) else route


def test_parse_source_maps_shortcuts_to_tags():
    assert parse_source("postgres") == ("story", "postgres")
    assert parse_source("@ask kafka streams") == ("ask_hn", "kafka streams")
    assert parse_source("@show") == ("show_hn", "")
    assert parse_source("@front") == ("front_page", "")


def test_discover_filters_old_own_and_low_point_stories(monkeypatch):
    monkeypatch.setenv("HN_USERNAME", "Me")
    api = FakeApi({"search_by_date": {"hits": [
        hit("1"), hit("2", author="me"), hit("3", hours_ago=100), hit("4", points=1), hit("1"),
        hit("5", url=None, text="How do you<p>retire flags?", title="Ask HN: flags"),
    ]}})
    threads = HackerNewsPlatform({"min_points": 2}, get_json=api).discover(
        ["@ask flags"], NOW - timedelta(hours=48), 30)
    assert [t.id for t in threads] == ["1", "5"]
    assert threads[0].body == "Link: https://example.com/post" and threads[0].source == "Ask HN flags"
    assert threads[0].url == "https://news.ycombinator.com/item?id=1"
    assert threads[1].body == "How do you\n\nretire flags?"
    params = api.calls[0][1]
    assert params["tags"] == "ask_hn" and params["query"] == "flags"
    assert params["numericFilters"].startswith("created_at_i>=")


def test_fetch_context_orders_comments_by_discussion_size():
    item = {"id": 1, "type": "story", "points": 42, "children": [
        {"author": "a", "text": "short", "children": []},
        {"author": "b", "text": "<p>big</p>", "children": [{"text": "x", "children": [{"text": "y"}]}]},
        {"author": None, "text": None, "children": []},  # deleted
    ]}
    plat = HackerNewsPlatform({}, get_json=FakeApi({"items/1": item}))
    t = plat.fetch_context(plat._to_thread(hit("1"), "postgres"))
    assert [c.author for c in t.top_comments] == ["b", "a"]
    assert t.score == 42 and t.num_comments == 5


def test_thread_from_comment_url_replies_to_that_comment():
    story = {"id": 1, "type": "story", "title": "Postgres 19", "url": "https://x", "author": "op", "points": 9,
             "created_at_i": int(NOW.timestamp()), "children": [{"author": "c", "text": "hi", "children": []}]}
    comment = {"id": 7, "type": "comment", "story_id": 1, "author": "bob", "text": "Use <i>pg_repack</i>",
               "children": [{"author": "d", "text": "agreed"}]}
    plat = HackerNewsPlatform({}, get_json=FakeApi({"items/1": story, "items/7": comment}))
    t = plat.thread_from_url("https://news.ycombinator.com/item?id=7")
    assert t.id == "1" and t.url == "https://news.ycombinator.com/item?id=7"
    assert "replying to this comment by bob:\nUse pg_repack" in t.body
    assert [c.author for c in t.top_comments] == ["d"]
    assert plat.thread_from_url("https://example.com/item?id=7") is None


def test_hn_drafts_but_posting_is_manual():
    plat = get_platform("hackernews", {})
    assert plat.posting == "manual" and plat.constraints.ai_drafts_allowed and not plat.constraints.markdown
    with pytest.raises(WingmanError):
        plat.submit(None)
    with pytest.raises(WingmanError):
        plat.publish("hi")


def test_my_activity_pages_through_your_comments(monkeypatch):
    monkeypatch.setenv("HN_USERNAME", "me")

    def page(params):
        n = int(params["page"])
        return {"nbPages": 2, "hits": [{"objectID": str(n), "story_title": "Story", "comment_text": "c",
                                        "created_at_i": int(NOW.timestamp())}]}

    api = FakeApi({"search_by_date": page})
    items = HackerNewsPlatform({}, get_json=api).my_activity(10)
    assert [i.id for i in items] == ["0", "1"] and api.calls[0][1]["tags"] == "comment,author_me"
    monkeypatch.setenv("HN_USERNAME", "")
    with pytest.raises(WingmanError, match="HN_USERNAME"):
        HackerNewsPlatform({}, get_json=api).my_activity(10)


def test_hn_urls_map_to_hackernews():
    assert platform_for_url("https://news.ycombinator.com/item?id=123") == "hackernews"


def test_new_post_url_prefills_hn_submit_form():
    plat = HackerNewsPlatform({})
    assert plat.new_post_url("Ask HN: How do you retire feature flags?") == \
        "https://news.ycombinator.com/submitlink?t=Ask%20HN%3A%20How%20do%20you%20retire%20feature%20flags%3F"
    card = LinkCard("https://example.com/a?b=1", "Feature toggles " + "x" * 100)
    url = plat.new_post_url("ignored", card)
    assert "u=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1" in url and len(parse_qs(urlsplit(url).query)["t"][0]) == 80


def test_manual_new_post_copies_text_and_opens_the_submit_form(store, monkeypatch):
    opened, copied = [], []
    monkeypatch.setattr("wingman.core.review.webbrowser.open", opened.append)
    monkeypatch.setattr("wingman.core.review.clipboard.copy", lambda t: copied.append(t) or True)
    monkeypatch.setattr("wingman.ui.ask", lambda *a, **k: "https://news.ycombinator.com/item?id=9")
    session = ReviewSession(HackerNewsPlatform({}), store, None, PacingSettings(), [], new_post=True)
    target = compose_target("hackernews", "Ask HN: feature flags")
    store.save_pasted(target)
    assert session._post(target, "We have 300 flags.") == "posted"
    assert copied == ["We have 300 flags."] and opened[0].startswith("https://news.ycombinator.com/submitlink?t=Ask")
    assert store.logged_posts("hackernews")[0].source == "your feed"
