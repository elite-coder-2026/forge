"""The single rendering boundary: all terminal output and input goes through here.

Only this package imports rich or prompt_toolkit. Colors live in `theme.py`,
one component per module in `components/`.

Tool blocks (`render_tool_start` / `render_tool_result`) and diff panels are
drawn only on a terminal. Approvals go through `prompt_permission`.
"""

from __future__ import annotations

from .components.banner import render_banner
from .components.diff import render_diff
from .components.input_box import make_input, read_input
from .components.permission import prompt_permission
from .components.status import emit, render_error, render_status
from .components.tools import render_tool_result, render_tool_start, stop_tool_block
from .components.transcript import AssistantStream, render_assistant_stream, render_user

__all__ = [
    "AssistantStream", "ask", "emit", "make_input", "prompt_permission", "read_input",
    "render_assistant_stream", "render_banner", "render_diff", "render_error",
    "render_status", "render_tool_result", "render_tool_start", "render_user",
    "stop_tool_block",
]


def ask(prompt: str = "") -> str:
    """Read one plain line (raises EOFError/KeyboardInterrupt like input())."""
    return input(prompt)


