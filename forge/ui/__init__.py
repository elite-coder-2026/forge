"""The single rendering boundary: all terminal output and input goes through here.

Only this package imports rich or prompt_toolkit. Colors live in `theme.py`,
one component per module in `components/`.

`render_tool_start`, `render_tool_result`, `render_diff` and
`prompt_permission` are plain passthroughs for now: the agent loop doesn't
emit tool events yet, so there is nothing to draw (see the UI plan).
"""

from __future__ import annotations

from typing import Any, Optional

from .components.banner import render_banner
from .components.input_box import make_input, read_input
from .components.status import emit, render_error, render_status
from .components.transcript import AssistantStream, render_assistant_stream, render_user

__all__ = [
    "AssistantStream", "ask", "emit", "make_input", "prompt_permission", "read_input",
    "render_assistant_stream", "render_banner", "render_diff", "render_error",
    "render_status", "render_tool_result", "render_tool_start", "render_user",
]


def ask(prompt: str = "") -> str:
    """Read one plain line (raises EOFError/KeyboardInterrupt like input())."""
    return input(prompt)


def render_tool_start(name: str, target: str = "") -> None:
    emit(f"{name} {target}".rstrip())


def render_tool_result(name: str, output: str, ok: bool = True) -> None:
    emit(output)


def render_diff(path: str, diff: str) -> None:
    emit(diff)


def prompt_permission(question: str, choices: Optional[Any] = None) -> str:
    return ask(question)
