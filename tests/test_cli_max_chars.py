# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

from types import SimpleNamespace

from typer.testing import CliRunner

from wingman import cli
from wingman.config import DraftingSettings


class FakeServices:
    def __init__(self, **_):
        self.settings = SimpleNamespace(drafting=DraftingSettings())


def test_max_chars_overrides_the_configured_limit(monkeypatch):
    monkeypatch.setattr(cli, "Services", FakeServices)
    assert cli._services(600).settings.drafting.max_draft_chars == 600
    assert cli._services().settings.drafting.max_draft_chars == DraftingSettings().max_draft_chars


def test_max_chars_is_on_every_drafting_command():
    for command in ("draft", "review", "post"):
        result = CliRunner().invoke(cli.app, [command, "--help"], env={"COLUMNS": "200"})
        assert "--max-chars" in result.output, command


def test_max_chars_rejects_tiny_limits():
    result = CliRunner().invoke(cli.app, ["draft", "https://example.com", "--length", "50"])
    assert result.exit_code == 2
