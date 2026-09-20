"""Tool call blocks: a spinner while a tool runs, then one compact status line
(plus trimmed output for shell commands and an optional diff panel).

Only drawn on a terminal. When output isn't a terminal these do nothing, so
piped and captured output is exactly what it was before tool blocks existed.
"""

from __future__ import annotations

import atexit
import re

from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text

from ..console import get_console
from .diff import render_diff

MAX_OUTPUT_LINES = 10

_live: Live | None = None


def _header(name: str, target: str, mark: str = "", style: str = "") -> Text:
    line = Text()
    if mark:
        line.append(f"{mark} ", style=style)
    line.append(name, style="tool.name")
    if target:
        line.append(f"  {target}", style="muted")
    return line


def render_tool_start(name: str, target: str = "") -> None:
    """Show a spinner for a running tool."""
    global _live
    console = get_console()
    if not console.is_terminal:
        return
    stop_tool_block()
    _live = Live(
        Spinner("dots", text=_header(name, target), style="accent"),
        console=console, transient=True, refresh_per_second=12,
    )
    _live.start()
    atexit.register(stop_tool_block)


def stop_tool_block() -> None:
    """Stop the spinner if one is running (also used when a turn is cancelled)."""
    global _live
    if _live is not None:
        _live.stop()
        _live = None


def _exit_code(output: str) -> int | None:
    match = re.match(r"exit code: (-?\d+)", output)
    return int(match.group(1)) if match else None


def render_tool_result(name: str, target: str, ok: bool, output: str = "", diff: str = "") -> None:
    """Replace the spinner with a status line. A shell result also shows its
    trimmed output and exit code; a non-empty `diff` shows as a panel."""
    console = get_console()
    stop_tool_block()
    if not console.is_terminal:
        return

    code = _exit_code(output)
    if code is not None:
        ok = code == 0
    console.print(_header(name, target, "✓" if ok else "✗", "tool.ok" if ok else "tool.fail"))

    if code is not None:
        body = output.splitlines()[1:]
        shown = body[-MAX_OUTPUT_LINES:]
        if len(body) > len(shown):
            console.print(Text(f"  … {len(body) - len(shown)} earlier lines", style="tool.output"))
        for line in shown:
            console.print(Text(f"  {line}", style="tool.output"), soft_wrap=True)
        console.print(Text(f"  exit code {code}", style="tool.ok" if ok else "tool.fail"))
    elif not ok and output:
        console.print(Text(f"  {output.splitlines()[0]}", style="tool.fail"), soft_wrap=True)

    if diff:
        render_diff(target, diff)
