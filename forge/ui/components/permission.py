"""Approval prompt: a bordered panel describing an edit or command, then a
one-line choice (allow once / always this session / deny)."""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from ..console import get_console
from .diff import colored_lines

CHOICES = "[y] allow once   [a] always this session   [n] deny"


def _body(target: str, body: str, is_diff: bool) -> Group:
    parts = [Text(target, style="bold")]
    if body:
        parts.append(Text(""))
        parts.extend(colored_lines(body, is_diff))
    return Group(*parts)


def prompt_permission(title: str, target: str, body: str = "", is_diff: bool = False) -> str:
    """Show the panel and ask. Returns "once", "always" or "deny". A blank
    answer, Ctrl+D or an unrecognized key denies; Ctrl+C is left to propagate
    so it still cancels the turn."""
    console = get_console()
    console.print(
        Panel(
            _body(target, body, is_diff),
            title=Text(title, style="permission.title"),
            title_align="left",
            border_style="permission.border",
            padding=(0, 1),
        )
    )
    try:
        answer = input(f"{CHOICES}\n> ").strip().lower()
    except EOFError:
        answer = ""
    console.print()
    if answer in ("y", "yes"):
        return "once"
    if answer in ("a", "always"):
        return "always"
    return "deny"
