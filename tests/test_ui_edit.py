# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

import sys

import pytest

from wingman import ui
from wingman.errors import WingmanError

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX shell editors")


def _editor(monkeypatch, command):
    monkeypatch.delenv("VISUAL", raising=False)
    monkeypatch.setenv("EDITOR", command)


def test_edit_returns_saved_text(monkeypatch):
    _editor(monkeypatch, f"{sys.executable} -c \"import sys,pathlib;p=pathlib.Path(sys.argv[1]);"
                         f"p.write_text(p.read_text().upper())\"")
    assert ui.edit("hello\n") == "HELLO\n"


def test_edit_returns_none_when_not_saved(monkeypatch):
    _editor(monkeypatch, "true")
    assert ui.edit("hello") is None


def test_edit_reports_missing_editor(monkeypatch):
    _editor(monkeypatch, "no-such-editor-wingman")
    with pytest.raises(WingmanError, match="Couldn't run your editor"):
        ui.edit("hello")
