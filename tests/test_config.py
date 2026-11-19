# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

import os
import stat
import tomllib
from importlib import import_module

import pytest
from typer.testing import CliRunner

from wingman import cli
from wingman.config import Config, Settings, find_home, load_config
from wingman.core.retrieval import ollama_url
from wingman.errors import ConfigError
from wingman.platforms import check_settings, get_platform


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    cwd, xdg = tmp_path / "project", tmp_path / "xdg"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.delenv("WINGMAN_HOME", raising=False)
    return cwd, xdg / "wingman"


def test_home_prefers_env_then_cwd_config_then_user_config(dirs, monkeypatch, tmp_path):
    cwd, user = dirs
    assert find_home() == cwd  # nothing anywhere: current directory (where `init` would create files)
    user.mkdir(parents=True)
    (user / "wingman.toml").write_text("")
    assert find_home() == user
    (cwd / "wingman.toml").write_text("")
    assert find_home() == cwd
    monkeypatch.setenv("WINGMAN_HOME", str(tmp_path / "elsewhere"))
    assert find_home() == tmp_path / "elsewhere"


def _cfg(raw: dict) -> Config:
    return Config(home=os.getcwd(), settings=Settings.model_validate(raw))


def test_platform_name_uses_flag_then_default_then_first_enabled():
    cfg = _cfg({"platforms": {"reddit": {"enabled": False}, "mastodon": {}, "github": {}}})
    assert cfg.platform_name("bluesky") == "bluesky"
    assert cfg.platform_name(None) == "mastodon"
    assert _cfg({"default_platform": "github", "platforms": {"mastodon": {}}}).platform_name(None) == "github"
    with pytest.raises(ConfigError, match="No platform configured"):
        _cfg({}).platform_name(None)
    with pytest.raises(ConfigError, match="No platform enabled"):
        _cfg({"platforms": {"reddit": {"enabled": False}}}).platform_name(None)


def test_unknown_platform_settings_are_errors_with_a_hint():
    with pytest.raises(ConfigError, match=r"'max_age_hour' \(did you mean 'max_age_hours'\?\)"):
        check_settings("bluesky", {"max_age_hour": 12}, frozenset({"languages"}))
    with pytest.raises(ConfigError, match="Unknown setting in"):
        get_platform("github", {"skip_answerd": True})
    check_settings("bluesky", {"enabled": True, "sources": [], "languages": ["en"]}, frozenset({"languages"}))


def test_init_creates_config_from_templates_and_never_overwrites(dirs, monkeypatch):
    cwd, user = dirs
    result = CliRunner().invoke(cli.app, ["init"])
    assert result.exit_code == 0, result.output
    assert tomllib.loads((cwd / "wingman.toml").read_text())["platforms"]["github"]["enabled"] is False
    assert "ANTHROPIC_API_KEY=" in (cwd / ".env").read_text()
    if os.name != "nt":  # Windows has no POSIX file modes
        assert stat.S_IMODE((cwd / ".env").stat().st_mode) == 0o600
    (cwd / "wingman.toml").write_text("# mine")
    CliRunner().invoke(cli.app, ["init"])
    assert (cwd / "wingman.toml").read_text() == "# mine"
    result = CliRunner().invoke(cli.app, ["init", "--global"])
    assert result.exit_code == 0 and (user / "wingman.toml").exists()


def test_example_config_loads_and_every_platform_section_is_valid(dirs):
    cwd, _ = dirs
    CliRunner().invoke(cli.app, ["init"])
    cfg = load_config(cwd)
    for name, section in cfg.settings.platforms.items():  # validate keys without creating clients (no credentials)
        check_settings(name, section, import_module(f"wingman.platforms.{name}").SETTINGS)
    with pytest.raises(ConfigError, match="No platform enabled"):  # all platforms start disabled
        cfg.platform_name(None)


def test_ollama_url_follows_ollama_host(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert ollama_url() == "http://localhost:11434"
    monkeypatch.setenv("OLLAMA_HOST", "gpu-box:11434")
    assert ollama_url() == "http://gpu-box:11434"
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.example.com/")
    assert ollama_url() == "https://ollama.example.com"
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    assert ollama_url() == "http://localhost:11434"
