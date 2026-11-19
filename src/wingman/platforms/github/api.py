"""GitHub Discussions adapter: search discovery, discussion context, comments and your comment history."""

from __future__ import annotations

import logging
import re
from datetime import datetime

from wingman.core.models import ActivityItem, Comment, Constraints, LinkCard, Reply, Thread
from wingman.errors import WingmanError
from wingman.platforms.github.client import GraphQLClient, find_token

log = logging.getLogger(__name__)

MAX_COMMENT_CHARS = 65536
TOP_COMMENTS = 8
REPO = re.compile(r"^[\w.-]+/[\w.-]+$")
WEB_DISCUSSION_URL = re.compile(r"^https://github\.com/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/discussions/(?P<n>\d+)")

CONSTRAINTS = Constraints(
    max_length=MAX_COMMENT_CHARS,
    markdown=True,
    ai_drafts_allowed=True,
    notes=("GitHub Discussions: technical and precise. Markdown renders, so use code blocks for code, "
           "config and commands. Answer the actual question first, with concrete steps, versions and "
           "trade-offs; say so plainly when something depends on their setup or you're not sure. Links to "
           "official docs or issues are welcome; link your own writing only when it directly answers the "
           "question. No greetings, sign-offs or filler. In Q&A categories, aim to be the answer they mark."),
)

DISCUSSION_FIELDS = """
    id number title body url createdAt closed locked upvoteCount
    author { login }
    repository { nameWithOwner }
    category { name isAnswerable }
    answer { id }
    comments { totalCount }
"""

SEARCH = """
query($q: String!, $first: Int!) {
  search(query: $q, type: DISCUSSION, first: $first) {
    nodes { ... on Discussion { FIELDS } }
  }
}""".replace("FIELDS", DISCUSSION_FIELDS)

CONTEXT = """
query($id: ID!, $first: Int!) {
  node(id: $id) { ... on Discussion { FIELDS
    comments(first: $first) { totalCount nodes { author { login } body upvoteCount isAnswer } }
  } }
}""".replace("FIELDS", DISCUSSION_FIELDS.replace("comments { totalCount }", ""))

BY_NUMBER = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { discussion(number: $number) { FIELDS } }
}""".replace("FIELDS", DISCUSSION_FIELDS)

ADD_COMMENT = """
mutation($id: ID!, $body: String!) {
  addDiscussionComment(input: {discussionId: $id, body: $body}) { comment { url } }
}"""

MY_COMMENTS = """
query($login: String!, $first: Int!, $after: String) {
  user(login: $login) {
    repositoryDiscussionComments(first: $first, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id body url upvoteCount isAnswer createdAt discussion { repository { nameWithOwner } } }
    }
  }
}"""


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def search_query(source: str, since: datetime) -> str:
    """A repo ("owner/name") becomes repo:owner/name; anything else is used as a discussion search as-is."""
    scope = f"repo:{source}" if REPO.match(source) else source
    return f"{scope} created:>={since.strftime('%Y-%m-%dT%H:%M:%SZ')}"


class GitHubDiscussionsPlatform:
    name = "github"
    posting = "api"
    has_scores = True  # upvotes
    constraints = CONSTRAINTS

    def __init__(self, settings: dict, client: GraphQLClient | None = None):
        self.settings = settings
        self.client = client or GraphQLClient(find_token())
        self.skip_answered = settings.get("skip_answered", True)

    # ------------------------------------------------------------ conversion
    def _to_thread(self, d: dict) -> Thread:
        rules = [f"Category: {d['category']['name']}"
                 + (" (Q&A: the author can mark one reply as the answer)" if d["category"]["isAnswerable"] else "")]
        return Thread(
            platform=self.name, id=d["id"], source=d["repository"]["nameWithOwner"], title=d["title"],
            body=d.get("body") or "", url=d["url"], author=(d.get("author") or {}).get("login", "ghost"),
            score=d.get("upvoteCount", 0), num_comments=d.get("comments", {}).get("totalCount", 0),
            created_at=_time(d["createdAt"]), source_rules=rules,
        )

    def _wanted(self, d: dict, since: datetime) -> bool:
        return (
            bool(d.get("id"))                                                   # search can return other types
            and not d.get("closed") and not d.get("locked")
            and not (self.skip_answered and d.get("answer"))                    # already has a marked answer
            and (d.get("author") or {}).get("login") != self.client.viewer      # not your own
            and _time(d["createdAt"]) >= since
        )

    def _warn_disabled(self, repos: list[str]) -> None:
        """Repos with Discussions turned off just return nothing, so say so (one query for all of them)."""
        if not repos:
            return
        fields = " ".join(f'r{i}: repository(owner: "{r.split("/")[0]}", name: "{r.split("/")[1]}") '
                          "{ nameWithOwner hasDiscussionsEnabled }" for i, r in enumerate(repos))
        try:
            found = self.client.query(f"query {{ {fields} }}")
        except WingmanError as e:  # a typo'd repo fails the whole query; name it rather than fail the scan
            log.warning("Couldn't check repos in [platforms.github] sources: %s", e)
            return
        for repo, info in zip(repos, found.values(), strict=True):
            if info is None:
                log.warning("GitHub repo %s not found; check [platforms.github] sources.", repo)
            elif not info["hasDiscussionsEnabled"]:
                log.warning("%s doesn't use GitHub Discussions; remove it from [platforms.github] sources.", repo)

    # ------------------------------------------------------------ Platform
    def discover(self, sources: list[str], since: datetime, limit: int) -> list[Thread]:
        """Sources: repos ("fastapi/fastapi") or discussion searches ("org:apache kafka")."""
        self._warn_disabled([s for s in sources if REPO.match(s)])
        seen: dict[str, Thread] = {}
        for source in sources:
            nodes = self.client.query(SEARCH, q=search_query(source, since), first=min(limit, 100))["search"]["nodes"]
            for d in nodes:
                if d and d.get("id") not in seen and self._wanted(d, since):
                    seen[d["id"]] = self._to_thread(d)
        return list(seen.values())

    def fetch_context(self, thread: Thread) -> Thread:
        d = self.client.query(CONTEXT, id=thread.id, first=50)["node"]
        fresh = self._to_thread({**d, "comments": {"totalCount": d["comments"]["totalCount"]}})
        thread.score, thread.num_comments, thread.source_rules = fresh.score, fresh.num_comments, fresh.source_rules
        comments = sorted(d["comments"]["nodes"], key=lambda c: (c.get("isAnswer", False), c.get("upvoteCount", 0)),
                          reverse=True)
        thread.top_comments = [Comment(author=(c.get("author") or {}).get("login", "ghost"),
                                       body=("[marked as answer] " if c.get("isAnswer") else "") + c.get("body", ""),
                                       score=c.get("upvoteCount", 0)) for c in comments[:TOP_COMMENTS]]
        return thread

    def thread_from_url(self, url: str) -> Thread | None:
        m = WEB_DISCUSSION_URL.match(url)
        if not m:
            return None
        d = self.client.query(BY_NUMBER, owner=m.group("owner"), name=m.group("repo"),
                              number=int(m.group("n")))["repository"]["discussion"]
        return self.fetch_context(self._to_thread(d)) if d else None

    def submit(self, reply: Reply) -> str:
        out = self.client.query(ADD_COMMENT, id=reply.thread_id, body=reply.text)
        return out["addDiscussionComment"]["comment"]["url"]

    def publish(self, text: str, link: LinkCard | None = None) -> str:
        raise WingmanError("New posts aren't supported on GitHub Discussions; wingman only drafts replies there.")

    def my_activity(self, limit: int) -> list[ActivityItem]:
        items: list[ActivityItem] = []
        after = None
        while len(items) < limit:
            page = self.client.query(MY_COMMENTS, login=self.client.viewer, first=min(100, limit - len(items)),
                                     after=after)["user"]["repositoryDiscussionComments"]
            items += [ActivityItem(
                platform=self.name, id=c["id"], source=c["discussion"]["repository"]["nameWithOwner"],
                body=c.get("body", ""), score=c.get("upvoteCount", 0), created_at=_time(c["createdAt"]),
                permalink=c["url"]) for c in page["nodes"]]
            if not page["pageInfo"]["hasNextPage"]:
                break
            after = page["pageInfo"]["endCursor"]
        return items[:limit]
