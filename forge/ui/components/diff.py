"""Colored unified diffs, shared by the approval prompt and tool results."""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from ..console import get_console


def colored_lines(body: str, is_diff: bool = True) -> list[Text]:
    """One Text per line; for a diff, added/removed/hunk lines get their style."""
    lines = []
    for line in body.splitlines():
        style = ""
        if is_diff:
            if line.startswith("+"):
                style = "diff.add"
            elif line.startswith("-"):
                style = "diff.remove"
            elif line.startswith("@@"):
                style = "diff.hunk"
        lines.append(Text(line, style=style))
    return lines


def render_diff(path: str, diff: str) -> None:
    """A unified diff inside a bordered panel titled with the file path."""
    if not diff.strip():
        return
    get_console().print(
        Panel(
            Group(*colored_lines(diff)),
            title=Text(path, style="tool.name"),
            title_align="left",
            border_style="muted",
            padding=(0, 1),
        )
    )
