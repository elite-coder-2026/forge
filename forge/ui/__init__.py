"""The single rendering boundary: all terminal output and input goes through here.

Step 1 is a plain passthrough to print()/input(); later steps swap the
internals for rich / prompt_toolkit components without touching callers.
"""

import sys
from typing import Iterable, Optional


def emit(message: str = "", *, err: bool = False, end: str = "\n", flush: bool = False) -> None:
    """Write a line (or fragment) to stdout, or stderr with err=True."""
    print(message, file=sys.stderr if err else sys.stdout, end=end, flush=flush)


def ask(prompt: str = "") -> str:
    """Read one line of typed input (raises EOFError/KeyboardInterrupt like input())."""
    return input(prompt)


def read_input(prompt: str = "") -> str:
    return ask(prompt)


def render_user(text: str) -> None:
    emit(text)


def render_assistant_stream(tokens: Iterable[str]) -> str:
    parts = []
    for token in tokens:
        parts.append(token)
        emit(token, end="", flush=True)
    return "".join(parts)


def render_status(message: str, *, err: bool = True) -> None:
    emit(message, err=err)


def render_error(message: str) -> None:
    emit(message, err=True)


def render_tool_start(name: str, target: str = "") -> None:
    emit(f"{name} {target}".rstrip())


def render_tool_result(name: str, output: str, ok: bool = True) -> None:
    emit(output)


def render_diff(path: str, diff: str) -> None:
    emit(diff)


def prompt_permission(question: str, choices: Optional[str] = None) -> str:
    return ask(question)
