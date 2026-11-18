from datetime import UTC, datetime, timedelta

import pytest

from wingman.core.models import Reply
from wingman.platforms import platform_for_url
from wingman.platforms.bluesky.api import BlueskyPlatform, web_url
from wingman.platforms.bluesky.richtext import grapheme_length, link_facets

ME = "did:plc:me"
NOW = datetime.now(UTC)


def post(rkey, text, did="did:plc:alice", handle="alice.bsky.social", hours_ago=1, reply=None, likes=0,
         langs=("en",)):
    record = {"text": text, "createdAt": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
              "langs": list(langs)}
    if reply:
        record["reply"] = reply
    return {"uri": f"at://{did}/app.bsky.feed.post/{rkey}", "cid": f"cid-{rkey}",
            "author": {"did": did, "handle": handle}, "record": record,
            "likeCount": likes, "replyCount": 2, "indexedAt": record["createdAt"]}


class FakeClient:
    did, handle = ME, "me.bsky.social"

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, method, **params):
        self.calls.append((method, params))
        return self.responses[method]

    def post(self, method, body):
        self.calls.append((method, body))
        return {"uri": f"at://{ME}/app.bsky.feed.post/new1", "cid": "cid-new1"}


def test_link_facets_use_utf8_byte_offsets_and_drop_trailing_punctuation():
    text = "Café notes: https://kamranzafar.org/blog/x/."
    [facet] = link_facets(text)
    start = len("Café notes: ".encode())
    assert facet["index"] == {"byteStart": start, "byteEnd": start + len(b"https://kamranzafar.org/blog/x/")}
    assert facet["features"][0]["uri"] == "https://kamranzafar.org/blog/x/"


def test_grapheme_length_counts_what_people_see():
    assert grapheme_length("hello") == 5
    assert grapheme_length("café") == 4 and grapheme_length("café") == 4  # precomposed and combining accent
    assert grapheme_length("👍🏽") == 1  # emoji + skin tone modifier
    assert grapheme_length("👨‍💻") == 1  # ZWJ sequence


def test_discover_keeps_fresh_top_level_posts_in_wanted_languages():
    client = FakeClient({"app.bsky.feed.searchPosts": {"posts": [
        post("a1", "How do you retire old feature flags?\nWe have 300."),
        post("a2", "a reply", reply={"root": {}, "parent": {}}),
        post("a3", "my own post", did=ME),
        post("a4", "Wie entfernt man Feature Flags?", langs=("de",)),
        post("a5", "old question", hours_ago=72),
        post("a6", "   "),  # image-only post
    ]}})
    plat = BlueskyPlatform({"languages": ["en"]}, client=client)
    [t] = plat.discover(["feature flags"], NOW - timedelta(hours=24), 25)
    assert t.title == "How do you retire old feature flags?" and t.source == "feature flags"
    assert t.url == "https://bsky.app/profile/alice.bsky.social/post/a1"
    assert client.calls[0][1]["sort"] == "latest"


def test_reply_to_a_reply_keeps_the_thread_root_and_adds_link_facets():
    root = {"uri": "at://did:plc:bob/app.bsky.feed.post/root", "cid": "cid-root"}
    parent = post("p1", "follow-up question", reply={"root": root, "parent": root})
    client = FakeClient({"app.bsky.feed.getPosts": {"posts": [parent]}})
    url = BlueskyPlatform({}, client=client).submit(
        Reply("bluesky", parent["uri"], "feature flags", "Wrote about this: https://kamranzafar.org/blog/x/"))
    method, body = client.calls[-1]
    record = body["record"]
    assert method == "com.atproto.repo.createRecord" and body["repo"] == ME
    assert record["reply"] == {"root": root, "parent": {"uri": parent["uri"], "cid": parent["cid"]}}
    assert record["facets"][0]["features"][0]["uri"] == "https://kamranzafar.org/blog/x/"
    assert url == "https://bsky.app/profile/me.bsky.social/post/new1"


def test_thread_from_url_resolves_handle_and_loads_top_replies():
    question = post("q1", "Debezium keeps re-snapshotting after restart?", handle="bob.dev", did="did:plc:bob")
    client = FakeClient({
        "com.atproto.identity.resolveHandle": {"did": "did:plc:bob"},
        "app.bsky.feed.getPosts": {"posts": [question]},
        "app.bsky.feed.getPostThread": {"thread": {"post": question, "replies": [
            {"post": post("r1", "check snapshot.mode", likes=1)},
            {"post": post("r2", "offsets topic retention?", likes=9)},
        ]}},
    })
    t = BlueskyPlatform({}, client=client).thread_from_url("https://bsky.app/profile/bob.dev/post/q1")
    assert t.id == "at://did:plc:bob/app.bsky.feed.post/q1"
    assert [c.body for c in t.top_comments] == ["offsets topic retention?", "check snapshot.mode"]  # by likes
    assert BlueskyPlatform({}, client=client).thread_from_url("https://example.com/x") is None


@pytest.mark.parametrize(("url", "platform"), [
    ("https://bsky.app/profile/bob.dev/post/q1", "bluesky"),
    ("https://www.reddit.com/r/java/comments/abc/x/", "reddit"),
    ("https://example.com/forum/1", None),
])
def test_platform_is_detected_from_the_url(url, platform):
    assert platform_for_url(url) == platform


def test_web_url():
    assert web_url("me.bsky.social", "at://did:plc:me/app.bsky.feed.post/3kx") == \
        "https://bsky.app/profile/me.bsky.social/post/3kx"


def test_too_long_message_names_blueskys_limit(store):
    from wingman.config import PacingSettings
    from wingman.core.review import ReviewSession

    session = ReviewSession(BlueskyPlatform({}, client=FakeClient({})), store, None, PacingSettings(), [],
                            max_chars=1500)
    assert session._blocked("x" * 340).startswith("Too long: 340 characters; Bluesky's limit is 300.")
    assert session._blocked("x" * 300) is None
