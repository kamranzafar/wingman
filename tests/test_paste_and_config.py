import pytest

from wingman.config import load_config
from wingman.core.paste import EDITOR_TEMPLATE, parse_template, parse_url, to_thread
from wingman.errors import ConfigError


@pytest.mark.parametrize(("url", "community", "thread_id"), [
    ("https://www.reddit.com/r/apachekafka/comments/1abc2de/debezium_lag/?share=1", "apachekafka", "1abc2de"),
    ("https://old.reddit.com/r/java/comments/xyz9/t/", "java", "xyz9"),
])
def test_parse_url_reads_community_and_id(url, community, thread_id):
    assert parse_url(url) == (community, thread_id)


def test_parse_url_falls_back_to_a_stable_hash_for_share_links():
    community, thread_id = parse_url("https://www.reddit.com/r/java/s/AbCdEf")
    assert community is None and thread_id == parse_url("https://www.reddit.com/r/java/s/AbCdEf")[1]


def test_parse_template_reads_all_sections():
    text = (EDITOR_TEMPLATE
            .replace("== TITLE ==\n", "== TITLE ==\nConnector lag\n")
            .replace("== POST ==\n", "== POST ==\nIt lags after restarts.\n")
            .replace("only ---) ==\n", "only ---) ==\nCheck snapshot mode\n---\nRaise batch size\n")
            .replace("one per line) ==\n", "one per line) ==\n- No self-promotion\n"))
    x = parse_template(text)
    assert (x.found_thread, x.title, x.body) == (True, "Connector lag", "It lags after restarts.")
    assert [r.body for r in x.top_replies] == ["Check snapshot mode", "Raise batch size"]
    assert x.rules == ["No self-promotion"]
    t = to_thread("reddit", "https://u", "apachekafka", "id1", x)
    assert t.num_comments == 2 and t.source_rules == ["No self-promotion"]


def test_empty_template_finds_no_thread():
    assert not parse_template(EDITOR_TEMPLATE).found_thread


def test_config_defaults_apply_when_toml_is_missing(tmp_path):
    s = load_config(tmp_path).settings
    assert (s.drafting.drafts, s.llm.draft_model, s.pacing.max_per_hour) == (2, "claude-sonnet-5-5", 3)


def test_config_rejects_unknown_keys(tmp_path):
    (tmp_path / "wingman.toml").write_text("[drafting]\ndraft = 3\n")  # typo for `drafts`
    with pytest.raises(ConfigError, match=r"drafting\.draft"):
        load_config(tmp_path)


def test_config_rejects_invalid_values(tmp_path):
    (tmp_path / "wingman.toml").write_text('[profile]\nlink_blog = "always"\n')
    with pytest.raises(ConfigError, match="link_blog"):
        load_config(tmp_path)


def test_platform_settings_errors_are_config_errors(tmp_path):
    (tmp_path / "wingman.toml").write_text("[platforms.reddit]\nenabled = false\n")
    cfg = load_config(tmp_path)
    with pytest.raises(ConfigError, match="disabled"):
        cfg.platform_settings("reddit")
    with pytest.raises(ConfigError, match=r"No \[platforms\.bluesky\]"):
        cfg.platform_settings("bluesky")
