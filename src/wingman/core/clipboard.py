# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""System clipboard access (macOS, Wayland, X11, Windows)."""

from __future__ import annotations

import shutil
import subprocess

_COPY = (["pbcopy"], ["wl-copy"], ["xclip", "-selection", "clipboard"], ["clip"])
_PASTE = (["pbpaste"], ["wl-paste", "--no-newline"], ["xclip", "-selection", "clipboard", "-o"],
          ["powershell", "-command", "Get-Clipboard"])


def copy(text: str) -> bool:
    for cmd in _COPY:
        if shutil.which(cmd[0]):
            subprocess.run(cmd, input=text.encode(), check=False)
            return True
    return False


def paste() -> str | None:
    for cmd in _PASTE:
        if shutil.which(cmd[0]):
            result = subprocess.run(cmd, capture_output=True, check=False)
            return result.stdout.decode("utf-8", errors="replace")
    return None
