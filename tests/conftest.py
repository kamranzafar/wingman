from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from wingman.core.models import Comment, Thread
from wingman.core.store import Store


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "test.db") as s:
        yield s


@pytest.fixture
def thread():
    return Thread(
        platform="reddit", id="abc123", source="apachekafka", title="Debezium re-snapshots after restart",
        body="Our connector snapshots the whole table again whenever it restarts.",
        url="https://www.reddit.com/r/apachekafka/comments/abc123/x/", author="op", score=5, num_comments=2,
        created_at=datetime(2026, 11, 18, 10, tzinfo=UTC),
        top_comments=[Comment("a", "Check snapshot.mode", 3)], source_rules=["No self-promotion"],
    )


class FakeStream:
    """Stands in for client.beta.messages.stream(...): records the request, returns a canned response."""

    def __init__(self, calls: list, response):
        self.calls, self.response = calls, response

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.response


def fake_response(parsed, model="claude-sonnet-5-5", stop_reason="end_turn", input_tokens=1000, output_tokens=500):
    return SimpleNamespace(model=model, stop_reason=stop_reason, parsed_output=parsed,
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens,
                                                 cache_creation_input_tokens=0, cache_read_input_tokens=0))


@pytest.fixture
def fake_client():
    """(client, calls, set_response): a fake Anthropic client for LLM tests."""
    calls: list = []
    stream = FakeStream(calls, fake_response(None))
    client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))

    def set_response(response):
        stream.response = response

    return client, calls, set_response
