# SPDX-FileCopyrightText: 2026 Kamran Zafar
# SPDX-License-Identifier: Apache-2.0

"""Terminal presentation: one themed console, status lines, tables, key bars and single-key input.

Every module prints through this, so wingman looks the same everywhere and colours carry meaning:
accent = what to look at, ok/warn/err = outcome, muted = secondary detail.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Sequence

from rich import box
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from wingman.errors import WingmanError

THEME = Theme({
    "accent": "bold cyan",
    "title": "bold",
    "muted": "grey58",
    "ok": "green",
    "warn": "yellow",
    "err": "bold red",
    "key": "bold black on grey82",
    "key.label": "default",
    "badge": "bold",
})

console = Console(theme=THEME, highlight=False)

WIDE = 140  # at or above this width, two drafts sit side by side


# ---------------------------------------------------------------- status lines


def ok(message: str) -> None:
    console.print(f"[ok]✓[/] {message}")


def warn(message: str) -> None:
    console.print(f"[warn]![/] {message}")


def error(message: str) -> None:
    console.print(f"[err]✗[/] {message}")


def info(message: str) -> None:
    console.print(f"[accent]›[/] {message}")


def muted(message: str) -> None:
    console.print(f"[muted]{message}[/]")


def step(message: str) -> None:
    """A progress line inside a longer operation (indented, muted)."""
    console.print(f"  [muted]·[/] {message}")


def heading(title: str, right: str = "") -> None:
    """A section rule: `title ──────── right`."""
    console.print()
    console.rule(Text.assemble((f" {title} ", "title"), (f" {right} " if right else "", "muted")), align="left",
                 style="muted")


# ---------------------------------------------------------------- tables

LABEL_WIDTH = 8


def meta(rows: Iterable[tuple[str | Text, Text | str]]) -> None:
    """Aligned 'label  value' rows (muted labels), e.g. Angle / Replies / Sources."""
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="muted", no_wrap=True, min_width=LABEL_WIDTH)
    grid.add_column()
    for label, value in rows:
        grid.add_row(label, value)
    if grid.row_count:
        console.print(grid)


def table(title: str | None = None, *columns: str, right: Iterable[str] = ()) -> Table:
    """A consistently styled table; columns named in `right` are right-aligned (numbers)."""
    right = set(right)
    t = Table(title=title, title_justify="left", title_style="title", box=box.SIMPLE_HEAD,
              header_style="muted", pad_edge=False, show_edge=False)
    for col in columns:
        t.add_column(col, justify="right" if col in right else "left")
    return t


def bar(value: float, width: int = 12, style: str = "accent") -> Text:
    """A small horizontal bar for 0-1 values (shares, scores)."""
    filled = round(max(0.0, min(1.0, value)) * width)
    return Text.assemble(("█" * filled, style), ("░" * (width - filled), "muted"))


# ---------------------------------------------------------------- key bars & input


def keycap(key: str, label: str) -> Text:
    return Text.assemble((f" {key} ", "key"), " ", (label, "key.label"))


def keybar(*groups: Sequence[tuple[str, str]], width: int | None = None) -> Text:
    """Keys grouped by purpose, e.g. ([("1-2", "choose"), ("e", "edit")], [("p", "post")]).

    Wraps to fit the terminal: a group that doesn't fit moves to the next line whole, and a key
    is never separated from its label.
    """
    width = width or console.width
    gap, divider = Text("  "), Text("  │  ", style="muted")
    out, line = Text(no_wrap=True), 0

    def group_width(group: Sequence[tuple[str, str]]) -> int:
        return sum(len(keycap(k, label)) for k, label in group) + len(gap) * (len(group) - 1)

    for g, group in enumerate(groups):
        if line and line + len(divider) + group_width(group) > width:
            out.append("\n")
            line = 0
        elif g:
            out.append_text(divider)
            line += len(divider)
        for k, (key_, label) in enumerate(group):
            cap = keycap(key_, label)
            if k and line + len(gap) + len(cap) > width:  # a single group wider than the terminal
                out.append("\n")
                line = 0
            elif k:
                out.append_text(gap)
                line += len(gap)
            out.append_text(cap)
            line += len(cap)
    return out


def _getch() -> str:
    """One keypress without waiting for Enter (standard library only)."""
    if os.name == "nt":
        import msvcrt

        return msvcrt.getwch()
    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("WINGMAN_LINE_INPUT")


def interactive() -> bool:
    """True on a real terminal (single-key input and screen paging make sense)."""
    return _interactive()


def key(prompt: str, valid: str | Iterable[str], default: str | None = None) -> str:
    """Read one keypress (no Enter needed) from `valid`; Enter returns `default` if there is one.

    Falls back to line input when not on a terminal (pipes, tests) or with WINGMAN_LINE_INPUT=1.
    Ctrl-C / Ctrl-D raise KeyboardInterrupt / EOFError as usual.
    """
    valid = set(valid)
    suffix = f" [muted](enter = {default})[/]" if default else ""
    if not _interactive():
        while True:
            answer = Prompt.ask(f"{prompt}{suffix}", default=default or "", show_default=False,
                                console=console).strip().lower()
            if answer in valid or (answer == "" and default is not None):
                return answer or default
    console.print(f"{prompt}{suffix} ", end="")
    while True:
        ch = _getch()
        if ch in ("\x03",):
            raise KeyboardInterrupt
        if ch in ("\x04",):
            raise EOFError
        if ch in ("\r", "\n") and default is not None:
            console.print(f"[accent]{default}[/]")
            return default
        if ch.lower() in valid:
            console.print(f"[accent]{ch.lower()}[/]")
            return ch.lower()


def confirm(question: str, default: bool = False) -> bool:
    """Single-key yes/no; the capitalised option is what Enter picks."""
    choice = key(f"{question} [muted]{'Y/n' if default else 'y/N'}[/]", "yn", default="y" if default else "n")
    return choice == "y"


def ask(prompt: str, default: str = "") -> str:
    """Free-text input (instructions, links): a normal line prompt."""
    return Prompt.ask(f"[accent]›[/] {prompt}", default=default, show_default=bool(default), console=console)


def edit(text: str, extension: str = ".md") -> str | None:
    """Open `text` in $VISUAL/$EDITOR and return the result, or None if it wasn't saved."""
    import shlex
    import subprocess
    import tempfile
    from pathlib import Path

    windows = sys.platform == "win32"
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ("notepad" if windows else "vi")
    fd, name = tempfile.mkstemp(suffix=extension, prefix="wingman-")
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        before = path.stat().st_mtime_ns
        try:
            subprocess.run([*shlex.split(editor, posix=not windows), str(path)], check=True)
        except (OSError, subprocess.CalledProcessError) as exc:
            raise WingmanError(f"Couldn't run your editor ({editor}): {exc}") from exc
        if path.stat().st_mtime_ns == before:
            return None
        return path.read_text(encoding="utf-8")
    finally:
        path.unlink(missing_ok=True)
