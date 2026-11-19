import json

import pytest

from wingman.config import PacingSettings
from wingman.core.models import Document
from wingman.core.review import ReviewSession
from wingman.platforms.reddit.paste import RedditPastePlatform
from wingman.sources import github
from wingman.sources.web import extract_text, post_links, url_date


def test_url_date():
    assert url_date("https://x.org/blog/2026/10/30/feature-toggles/") == "2026-10-30"
    assert url_date("https://x.org/2016/08/spring-boot/") == "2016-08-01"
    assert url_date("https://cloudkit.dev/") == ""


def test_extract_text_uses_a_single_article_but_main_for_card_layouts():
    post = "<html><title>T</title><body><nav>menu</nav><article>Post body</article><aside>x</aside></body></html>"
    assert extract_text(post) == ("T", "Post body")
    cards = "<html><body><main><h1>Cloudkit</h1><article>Card 1</article><article>Card 2</article></main></body></html>"
    assert extract_text(cards)[1] == "Cloudkit\nCard 1\nCard 2"


def test_post_links_skips_tags_feeds_assets_and_other_sites():
    html = ('<a href="/blog/2026/10/30/post/">p</a><a href="/tag/java/">t</a><a href="/index.xml">f</a>'
            '<a href="https://other.com/a">o</a><a href="/blog/">i</a><a href="/img.png">i</a>')
    assert post_links(html, "https://me.dev/blog/") == ["https://me.dev/blog/2026/10/30/post/"]


def test_github_falls_back_to_cache_when_rate_limited(tmp_path, monkeypatch):
    cache = tmp_path / "github.json"
    docs = [Document("GitHub profile: me", "bio", "github"), Document("GitHub repo: jcl", "readme", "github")]
    monkeypatch.setattr(github, "fetch_github", lambda user, n: (docs[0], docs[1:]))
    assert github.GitHubSource("me", 10, cache).load()[1] == "1 GitHub READMEs"
    assert len(json.loads(cache.read_text())) == 2

    def rate_limited(user, n):
        raise RuntimeError("HTTP Error 403: rate limit exceeded")

    monkeypatch.setattr(github, "fetch_github", rate_limited)
    loaded, note = github.GitHubSource("me", 10, cache).load()
    assert loaded == docs and "cached" in note
    cache.unlink()
    with pytest.raises(RuntimeError, match="rate limit"):
        github.GitHubSource("me", 10, cache).load()


@pytest.fixture
def session(store):
    return ReviewSession(RedditPastePlatform({}), store, drafter=None, pacing=PacingSettings(),
                         private_terms=["ACME"], max_chars=1500)


@pytest.mark.parametrize(("text", "problem"), [
    ("We did this at ACME.", "ACME"),
    ("x" * 1501, "Too long: 1,501 characters; your max_draft_chars is 1,500"),
    ("x" * 1500, None),
    ("In my case [your experience with Kafka here].", "placeholder"),
    ("Set snapshot.mode=when_needed.", None),
])
def test_posting_guards(session, text, problem):
    blocked = session._blocked(text)
    assert (blocked is None) if problem is None else (problem in blocked)


def test_manual_posting_copies_opens_and_records(session, thread, monkeypatch):
    import wingman.core.review as review
    copied, opened = [], []
    monkeypatch.setattr(review.clipboard, "copy", lambda t: copied.append(t) or True)
    monkeypatch.setattr(review.webbrowser, "open", opened.append)
    monkeypatch.setattr("wingman.ui.ask", lambda *a, **k: "https://www.reddit.com/r/x/comments/abc123/c/k1")
    session.store.save_pasted(thread)  # `wingman draft` saves the thread before review
    assert session._post(thread, "Set snapshot.mode=when_needed.")
    assert copied == ["Set snapshot.mode=when_needed."] and opened == [thread.url]
    assert session.store.thread_status(thread.key) == "posted"
    assert session.store.logged_posts("reddit")[0].permalink.endswith("/k1")


def test_manual_posting_answer_x_records_nothing(session, thread, monkeypatch):
    import wingman.core.review as review
    monkeypatch.setattr(review.clipboard, "copy", lambda t: True)
    monkeypatch.setattr(review.webbrowser, "open", lambda u: None)
    monkeypatch.setattr("wingman.ui.ask", lambda *a, **k: "x")
    assert not session._post(thread, "ok")
    assert session.store.logged_posts("reddit") == []
