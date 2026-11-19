from datetime import UTC, datetime, timedelta

import pytest

from wingman.core.models import LinkCard, Reply
from wingman.errors import ConfigError
from wingman.platforms import platform_for_url
from wingman.platforms.mastodon.api import MastodonPlatform
from wingman.platforms.mastodon.client import MastodonClient
from wingman.platforms.mastodon.text import html_to_text, status_length

NOW = datetime.now(UTC)
ME = {"id": "1", "acct": "me", "url": "https://fosstodon.org/@me"}


def status(sid, content="<p>How do you retire old feature flags?</p><p>We have 300.</p>", acct="alice@hachyderm.io",
           account_id="2", hours_ago=1, bot=False, reply_to=None, reblog=None, lang="en", favs=0, visibility="public"):
    return {"id": sid, "uri": f"https://x/statuses/{sid}", "url": f"https://hachyderm.io/@alice/{sid}",
            "content": content, "created_at": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
            "account": {"id": account_id, "acct": acct, "bot": bot}, "in_reply_to_id": reply_to, "reblog": reblog,
            "language": lang, "favourites_count": favs, "replies_count": 0, "visibility": visibility,
            "spoiler_text": ""}


class FakeClient:
    me = ME

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, path, **params):
        self.calls.append((path, params))
        return self.responses[path]

    def post(self, path, body):
        self.calls.append((path, body))
        return {"id": "99", "uri": "https://fosstodon.org/users/me/statuses/99", "url": "https://fosstodon.org/@me/99"}


def test_html_to_text_keeps_paragraphs_full_links_mentions_and_hashtags():
    html = ('<p>Hi <span class="h-card"><a href="https://h.io/@bob" class="u-url mention">@<span>bob</span></a>'
            '</span> see <a href="https://kamranzafar.org/blog/flags/" rel="nofollow">'
            '<span class="invisible">https://</span><span class="ellipsis">kamranzafar.org/blo</span>'
            '<span class="invisible">g/flags/</span></a></p><p>line<br />two &amp; more '
            '<a href="https://h.io/tags/java" class="mention hashtag">#<span>java</span></a></p>')
    assert html_to_text(html) == "Hi @bob see https://kamranzafar.org/blog/flags/\n\nline\ntwo & more #java"


def test_status_length_counts_links_as_23_and_remote_mentions_by_username():
    assert status_length("see https://kamranzafar.org/a/very/long/path/that/goes/on/and/on") == 4 + 23
    assert status_length("@bob@hachyderm.io hi") == len("@bob hi")


def test_discover_keeps_fresh_human_top_level_posts_in_wanted_languages():
    client = FakeClient({"/api/v1/timelines/tag/featureflags": [
        status("a1"),
        status("a2", reply_to="a0"),
        status("a3", account_id=ME["id"]),
        status("a4", lang="de"),
        status("a5", hours_ago=72),
        status("a6", bot=True),
        status("a7", reblog={"id": "x"}),
        status("a8", visibility="private"),
    ]})
    [t] = MastodonPlatform({"languages": ["en"]}, client=client).discover(["#featureflags"], NOW - timedelta(hours=24),
                                                                         40)
    assert (t.id, t.source, t.author) == ("a1", "#featureflags", "alice@hachyderm.io")
    assert t.title == "How do you retire old feature flags?" and t.body.endswith("We have 300.")
    assert t.url == "https://hachyderm.io/@alice/a1"


def test_discover_routes_sources_to_the_right_endpoints():
    client = FakeClient({"/api/v1/timelines/home": [], "/api/v1/timelines/public": [],
                         "/api/v2/search": {"statuses": [status("s1")]}})
    threads = MastodonPlatform({}, client=client).discover(["@home", "@local", "pgvector"], NOW - timedelta(hours=24),
                                                           40)
    assert [c[0] for c in client.calls] == ["/api/v1/timelines/home", "/api/v1/timelines/public", "/api/v2/search"]
    assert client.calls[1][1]["local"] == "true" and client.calls[2][1]["type"] == "statuses"
    assert [t.id for t in threads] == ["s1"]


def test_fetch_context_keeps_direct_replies_best_first():
    client = FakeClient({
        "/api/v1/statuses/a1": status("a1", favs=5),
        "/api/v1/statuses/a1/context": {"descendants": [
            status("r1", "<p>meh</p>", acct="c", reply_to="a1", favs=1),
            status("r2", "<p>use expiry dates</p>", acct="d", reply_to="a1", favs=9),
            status("r3", "<p>nested</p>", acct="e", reply_to="r2", favs=50),
        ]},
    })
    t = MastodonPlatform({}, client=client).fetch_context(MastodonPlatform({}, client=client)._to_thread(
        status("a1"), "#x"))
    assert [c.author for c in t.top_comments] == ["d", "c"] and t.score == 5 and t.num_comments == 2


def test_submit_mentions_the_author_and_threads_the_reply():
    client = FakeClient({"/api/v1/statuses/a1": status("a1")})
    url = MastodonPlatform({"languages": ["en"]}, client=client).submit(
        Reply("mastodon", "a1", "#featureflags", "Expiry dates on every flag helped us."))
    path, body = client.calls[-1]
    assert path == "/api/v1/statuses" and url == "https://fosstodon.org/@me/99"
    assert body == {"status": "@alice@hachyderm.io Expiry dates on every flag helped us.", "in_reply_to_id": "a1",
                    "visibility": "public", "language": "en"}


def test_submit_does_not_double_the_mention():
    client = FakeClient({"/api/v1/statuses/a1": status("a1")})
    MastodonPlatform({}, client=client).submit(Reply("mastodon", "a1", "x", "@alice@hachyderm.io agreed"))
    assert client.calls[-1][1]["status"] == "@alice@hachyderm.io agreed"


def test_publish_appends_the_link_once_for_the_preview_card():
    client = FakeClient({})
    plat = MastodonPlatform({"visibility": "unlisted"}, client=client)
    plat.publish("New post on flags", LinkCard("https://kamranzafar.org/blog/flags/", "Flags"))
    plat.publish("Read https://kamranzafar.org/blog/flags/", LinkCard("https://kamranzafar.org/blog/flags/", "Flags"))
    assert client.calls[0][1]["status"] == "New post on flags\n\nhttps://kamranzafar.org/blog/flags/"
    assert client.calls[1][1]["status"] == "Read https://kamranzafar.org/blog/flags/"
    assert client.calls[0][1]["visibility"] == "unlisted"


def test_my_activity_pages_through_your_statuses():
    pages = iter([[status("3", account_id="1", favs=4), status("2", account_id="1", reply_to="x")], []])

    class Paging(FakeClient):
        def get(self, path, **params):
            self.calls.append((path, params))
            return next(pages)

    client = Paging({})
    items = MastodonPlatform({}, client=client).my_activity(limit=100)
    assert [(i.id, i.source, i.score) for i in items] == [("3", "posts", 4), ("2", "replies", 0)]
    assert client.calls[1][1]["max_id"] == "2" and client.calls[0][1]["exclude_reblogs"] == "true"


def test_constraints_leave_room_for_the_mention():
    assert MastodonPlatform({"max_characters": 1000}, client=FakeClient({})).constraints.max_length == 1000 - 32


def test_status_urls_from_any_instance_map_to_mastodon():
    assert platform_for_url("https://fosstodon.org/@kamran/113456789012345678") == "mastodon"
    assert platform_for_url("https://hachyderm.io/@bob@infosec.exchange/1234") == "mastodon"
    assert platform_for_url("https://bsky.app/profile/x/post/y") == "bluesky"
    assert platform_for_url("https://example.com/blog/post") is None


def test_client_explains_missing_settings():
    with pytest.raises(ConfigError, match="instance"):
        MastodonClient("", "tok")
    with pytest.raises(ConfigError, match="MASTODON_ACCESS_TOKEN"):
        MastodonClient("fosstodon.org", "")
    assert MastodonClient("fosstodon.org", "tok").instance == "https://fosstodon.org"
