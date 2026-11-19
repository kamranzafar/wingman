# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

from datetime import UTC, datetime, timedelta

import pytest

from wingman.core.models import Reply
from wingman.errors import ConfigError, WingmanError
from wingman.platforms import platform_for_url
from wingman.platforms.github import client as client_mod
from wingman.platforms.github.api import (
    ADD_COMMENT,
    BY_NUMBER,
    CONTEXT,
    MY_COMMENTS,
    SEARCH,
    GitHubDiscussionsPlatform,
    search_query,
)
from wingman.platforms.github.client import GraphQLClient

NOW = datetime.now(UTC)


def discussion(did, author="alice", hours_ago=2, answered=False, closed=False, locked=False, answerable=True,
               repo="spring-projects/spring-boot", comments=3, upvotes=1):
    return {"id": did, "number": 7, "title": f"How do I {did}?", "body": "Details **here**",
            "url": f"https://github.com/{repo}/discussions/7",
            "createdAt": (NOW - timedelta(hours=hours_ago)).isoformat().replace("+00:00", "Z"),
            "closed": closed, "locked": locked, "upvoteCount": upvotes, "author": {"login": author},
            "repository": {"nameWithOwner": repo}, "category": {"name": "Q&A", "isAnswerable": answerable},
            "answer": {"id": "x"} if answered else None, "comments": {"totalCount": comments}}


class FakeClient:
    viewer = "me"

    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def query(self, query, **variables):
        self.calls.append((query, variables))
        return self.responses[query] if not callable(self.responses[query]) else self.responses[query](variables)


def test_search_query_scopes_repos_and_passes_searches_through():
    since = datetime(2026, 11, 16, 9, 30, tzinfo=UTC)
    assert search_query("spring-projects/spring-boot", since) == \
        "repo:spring-projects/spring-boot created:>=2026-11-16T09:30:00Z"
    assert search_query("org:apache kafka", since) == "org:apache kafka created:>=2026-11-16T09:30:00Z"
    assert search_query("pgvector", since).startswith("pgvector ")


class RepoCheckClient(FakeClient):
    """Answers the repo check (any query not in `responses`) with each repo's Discussions setting."""

    def __init__(self, responses, enabled=None):
        super().__init__(responses)
        self.enabled = enabled or {}

    def query(self, query, **variables):
        if query in self.responses:
            return super().query(query, **variables)
        self.calls.append((query, variables))
        return {f"r{i}": None if on is None else {"nameWithOwner": repo, "hasDiscussionsEnabled": on}
                for i, (repo, on) in enumerate(self.enabled.items())}


def test_discover_warns_about_repos_without_discussions(caplog):
    client = RepoCheckClient({SEARCH: {"search": {"nodes": []}}},
                             enabled={"a/on": True, "b/off": False, "c/missing": None})
    GitHubDiscussionsPlatform({}, client=client).discover(["a/on", "b/off", "c/missing", "pgvector"],
                                                          NOW - timedelta(hours=72), 30)
    assert "b/off doesn't use GitHub Discussions" in caplog.text and "c/missing not found" in caplog.text
    assert "a/on" not in caplog.text


def test_discover_skips_answered_closed_locked_own_and_old_discussions():
    client = RepoCheckClient({SEARCH: {"search": {"nodes": [
        discussion("d1"), discussion("d2", answered=True), discussion("d3", closed=True),
        discussion("d4", locked=True), discussion("d5", author="me"), discussion("d6", hours_ago=200),
        {},  # search can return non-discussion nodes as empty objects
    ]}}}, enabled={"spring-projects/spring-boot": True})
    [t] = GitHubDiscussionsPlatform({}, client=client).discover(["spring-projects/spring-boot"],
                                                                NOW - timedelta(hours=72), 30)
    assert (t.id, t.source, t.author, t.num_comments) == ("d1", "spring-projects/spring-boot", "alice", 3)
    assert t.source_rules == ["Category: Q&A (Q&A: the author can mark one reply as the answer)"]
    assert client.calls[1][1]["q"].startswith("repo:spring-projects/spring-boot created:>=")


def test_skip_answered_can_be_turned_off():
    client = RepoCheckClient({SEARCH: {"search": {"nodes": [discussion("d2", answered=True)]}}},
                             enabled={"x/y": True})
    threads = GitHubDiscussionsPlatform({"skip_answered": False}, client=client).discover(
        ["x/y"], NOW - timedelta(hours=72), 30)
    assert [t.id for t in threads] == ["d2"]


def test_fetch_context_puts_the_marked_answer_first_then_upvotes():
    node = {**discussion("d1", upvotes=4), "comments": {"totalCount": 3, "nodes": [
        {"author": {"login": "a"}, "body": "meh", "upvoteCount": 1, "isAnswer": False},
        {"author": None, "body": "popular", "upvoteCount": 9, "isAnswer": False},
        {"author": {"login": "c"}, "body": "the fix", "upvoteCount": 2, "isAnswer": True},
    ]}}
    plat = GitHubDiscussionsPlatform({}, client=FakeClient({CONTEXT: {"node": node}}))
    t = plat.fetch_context(plat._to_thread(discussion("d1")))
    assert [c.author for c in t.top_comments] == ["c", "ghost", "a"]
    assert t.top_comments[0].body == "[marked as answer] the fix" and t.score == 4 and t.num_comments == 3


def test_thread_from_url_reads_the_discussion_by_number():
    client = FakeClient({
        BY_NUMBER: {"repository": {"discussion": discussion("d1")}},
        CONTEXT: {"node": {**discussion("d1"), "comments": {"totalCount": 0, "nodes": []}}},
    })
    t = GitHubDiscussionsPlatform({}, client=client).thread_from_url(
        "https://github.com/spring-projects/spring-boot/discussions/42")
    assert t.id == "d1" and client.calls[0][1] == {"owner": "spring-projects", "name": "spring-boot", "number": 42}
    assert GitHubDiscussionsPlatform({}, client=client).thread_from_url("https://github.com/x/y/issues/1") is None


def test_submit_adds_a_discussion_comment():
    client = FakeClient({ADD_COMMENT: {"addDiscussionComment": {"comment": {"url": "https://github.com/c/1"}}}})
    url = GitHubDiscussionsPlatform({}, client=client).submit(Reply("github", "d1", "x/y", "Use `@Lazy`."))
    assert url == "https://github.com/c/1" and client.calls[0][1] == {"id": "d1", "body": "Use `@Lazy`."}


def test_publish_is_not_supported():
    with pytest.raises(WingmanError, match="only drafts replies"):
        GitHubDiscussionsPlatform({}, client=FakeClient({})).publish("hi")


def test_my_activity_follows_cursors():
    def comment(cid):
        return {"id": cid, "body": "b", "url": f"https://github.com/c/{cid}", "upvoteCount": 2, "isAnswer": False,
                "createdAt": NOW.isoformat().replace("+00:00", "Z"),
                "discussion": {"repository": {"nameWithOwner": "x/y"}}}

    pages = {None: {"pageInfo": {"hasNextPage": True, "endCursor": "c1"}, "nodes": [comment("1")]},
             "c1": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [comment("2")]}}
    client = FakeClient({MY_COMMENTS: lambda v: {"user": {"repositoryDiscussionComments": pages[v["after"]]}}})
    items = GitHubDiscussionsPlatform({}, client=client).my_activity(limit=100)
    assert [(i.id, i.source, i.score) for i in items] == [("1", "x/y", 2), ("2", "x/y", 2)]


def test_discussion_urls_map_to_github_but_other_github_links_do_not():
    assert platform_for_url("https://github.com/debezium/debezium/discussions/123") == "github"
    assert platform_for_url("https://github.com/debezium/debezium") is None


def test_token_comes_from_env_then_gh_cli(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    assert client_mod.find_token() == "env-token"
    monkeypatch.setenv("GITHUB_TOKEN", "")
    monkeypatch.setattr(client_mod.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(client_mod.subprocess, "run",
                        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "gh-token\n"})())
    assert client_mod.find_token() == "gh-token"
    monkeypatch.setattr(client_mod.shutil, "which", lambda _: None)
    assert client_mod.find_token() == ""
    with pytest.raises(ConfigError, match="GITHUB_TOKEN"):
        GraphQLClient("")
