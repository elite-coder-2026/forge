"""Plain output, status lines and errors."""

from __future__ import annotations

from ..console import get_console


def emit(message: str = "", *, err: bool = False, end: str = "\n", flush: bool = False) -> None:
    """Write text verbatim: no markup, no highlighting, no wrapping."""
    console = get_console(err)
    console.print(message, end=end, markup=False, emoji=False, soft_wrap=True)
    if flush:
        console.file.flush()


def render_status(message: str, *, err: bool = True) -> None:
    get_console(err).print(message, style="status", markup=False, emoji=False, soft_wrap=True)


def render_error(message: str) -> None:
    get_console(True).print(message, style="error", markup=False, emoji=False, soft_wrap=True)
