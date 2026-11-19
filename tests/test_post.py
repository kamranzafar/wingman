import pytest

from tests.test_bluesky import ME, FakeClient
from wingman.config import PacingSettings
from wingman.core.composer import Composer, compose_target
from wingman.core.drafter import Draft, DraftingOptions, DraftSet
from wingman.core.models import Constraints, Document, LinkCard
from wingman.core.retrieval import Index
from wingman.core.review import ReviewSession
from wingman.errors import WingmanError
from wingman.platforms.bluesky.api import BlueskyPlatform
from wingman.platforms.reddit.paste import RedditPastePlatform
from wingman.sources.web import link_card

ARTICLE_URL = "https://example.com/blog/2026/10/30/feature-toggles-in-java-without-the-if-else/"


def test_publish_creates_a_top_level_post_with_link_card_and_clickable_link():
    client = FakeClient({})
    card = LinkCard(ARTICLE_URL, "Feature Toggles in Java, Without the If-Else", "One router per flag.")
    url = BlueskyPlatform({}, client=client).publish(f"New post: {ARTICLE_URL}", card)
    method, body = client.calls[-1]
    record = body["record"]
    assert method == "com.atproto.repo.createRecord" and body["repo"] == ME and "reply" not in record
    assert record["embed"]["external"] == {"uri": ARTICLE_URL, "title": card.title, "description": card.description}
    assert record["facets"][0]["features"][0]["uri"] == ARTICLE_URL
    assert url.startswith("https://bsky.app/profile/me.bsky.social/post/")


def test_reddit_refuses_new_posts():
    with pytest.raises(WingmanError, match="only drafts replies"):
        RedditPastePlatform({}).publish("hello")


def test_link_card_prefers_open_graph_tags():
    html = ('<html><head><title>Page title</title><meta property="og:title" content="OG title">'
            '<meta name="description" content="Plain description"></head></html>')
    assert link_card("https://x.dev/a", html) == LinkCard("https://x.dev/a", "OG title", "Plain description")
    assert link_card("https://x.dev/b", "<html></html>").title == "https://x.dev/b"


class RecordingLLM:
    def __init__(self):
        self.kwargs = None

    def model_for(self, task):
        return "claude-haiku-5-5"

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return DraftSet(should_reply=True, reason="", rule_concerns=[], drafts=[Draft(angle="a", text="t")],
                        claims_to_verify=[])


def test_composer_shares_the_article_and_skips_it_from_excerpts():
    article = Document("Feature Toggles in Java", "At ACME we routed every toggle through one class. " * 5,
                       "blog", url=ARTICLE_URL)
    index = Index.build([article, Document("togglekit", "Proxy-based feature toggle routing for Java. " * 5,
                                           "github", url="https://github.com/example/togglekit")], embedder=None)
    llm = RecordingLLM()
    options = DraftingOptions(num_drafts=2, private_terms=("ACME",), max_chars=1500)
    composer = Composer(llm, "Summary.", Constraints(max_length=300), options, index=index, article=article)
    result = composer.draft(compose_target("bluesky", "feature toggle routing"))

    prompt, system = llm.kwargs["prompt"], llm.kwargs["system"]
    assert f"Include this exact URL once in every draft: {ARTICLE_URL}" in prompt
    assert "about 255 characters; never more than 300" in system and "INCLUDING any link" in system
    assert "ACME" not in prompt + system
    assert result.sources == ["togglekit"]  # the article itself isn't repeated as an excerpt
    assert llm.kwargs["task"] == "draft"


def test_new_post_mode_publishes_and_checks_the_link(store, monkeypatch):
    client = FakeClient({})
    plat = BlueskyPlatform({}, client=client)
    card = LinkCard(ARTICLE_URL, "Feature Toggles in Java")
    session = ReviewSession(plat, store, None, PacingSettings(), [], new_post=True, link=card)
    target = compose_target("bluesky", "feature toggles")
    store.save_pasted(target)

    confirms = iter([False])  # "draft doesn't include the article link. Post anyway?" -> no
    monkeypatch.setattr("wingman.ui.confirm", lambda *a, **k: next(confirms))
    assert session._post(target, "A post without the link") is None
    assert client.calls == []

    monkeypatch.setattr("wingman.ui.confirm", lambda *a, **k: True)  # "Post now?" -> yes
    assert session._post(target, f"Toggles at a seam: {ARTICLE_URL}") == "posted"
    method, body = client.calls[-1]
    assert method == "com.atproto.repo.createRecord" and "reply" not in body["record"]
    assert store.logged_posts("bluesky")[0].source == "your feed"
