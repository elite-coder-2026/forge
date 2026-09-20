"""User and assistant messages in the transcript."""

from __future__ import annotations

import atexit
from typing import Iterable

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from ..console import get_console
from ..theme import CODE_THEME, USER_BORDER


def render_user(text: str) -> None:
    """A user message as a left-bordered block."""
    console = get_console()
    for line in text.splitlines() or [""]:
        block = Text()
        block.append(USER_BORDER, style="user.border")
        block.append(line, style="user.text")
        console.print(block)
    console.print()


class AssistantStream:
    """Streams an assistant reply token by token.

    On a terminal the text is re-rendered as Markdown (highlighted code
    fences) inside `rich.live.Live` as it grows. When output isn't a terminal
    the raw tokens are written as they arrive, exactly as before, so piped
    output and captured output are unchanged.
    """

    def __init__(self) -> None:
        self._console = get_console()
        self.live_mode = self._console.is_terminal
        self._parts: list[str] = []
        self._live: Live | None = None
        self.mid_line = False
        atexit.register(self.pause)

    def write(self, token: str) -> None:
        self.mid_line = not token.endswith("\n")
        if not self.live_mode:
            self._console.file.write(token)
            self._console.file.flush()
            return
        self._parts.append(token)
        if self._live is None:
            self._live = Live(
                self._render(), console=self._console, refresh_per_second=10,
                vertical_overflow="visible",
            )
            self._live.start()
        else:
            self._live.update(self._render())

    def _render(self) -> Markdown:
        return Markdown("".join(self._parts), code_theme=CODE_THEME)

    def pause(self) -> None:
        """End the current block so other output can print below it; the next
        token starts a fresh block."""
        if self._live is not None:
            self._live.stop()
            self._live = None
            self._parts = []
            self.mid_line = False
        elif not self.live_mode and self.mid_line:
            self._console.file.write("\n")
            self._console.file.flush()
            self.mid_line = False

    close = pause


def render_assistant_stream(tokens: Iterable[str]) -> str:
    """Stream an iterable of tokens; returns the full text."""
    stream = AssistantStream()
    parts = []
    try:
        for token in tokens:
            parts.append(token)
            stream.write(token)
    finally:
        stream.close()
    return "".join(parts)
