"""The input box: a multiline prompt_toolkit prompt with a rule above the input
and a rule under it (top of the toolbar), history, slash completion, a status
line and key hints."""

from __future__ import annotations

import os
import shutil
import sys
from typing import Any, Callable

from ..theme import PROMPT_MARKER, PROMPT_STYLES, RULE_CHAR
from .transcript import render_user

KEY_HINTS = "enter send · alt+enter newline · shift+tab mode · / commands · ctrl+d exit"


def _rule() -> str:
    return RULE_CHAR * max(10, shutil.get_terminal_size().columns - 1)


def _map_shift_enter() -> None:
    """Make Shift+Enter behave like Alt+Enter (a newline). Terminals that can
    tell Shift+Enter apart send one of these sequences; prompt_toolkit doesn't
    know them, so map each to Escape+Enter."""
    from prompt_toolkit.input.ansi_escape_sequences import ANSI_SEQUENCES
    from prompt_toolkit.keys import Keys

    for sequence in ("\x1b[13;2u", "\x1b[27;2;13~", "\x1b[13;2~"):
        ANSI_SEQUENCES[sequence] = (Keys.Escape, Keys.ControlM)


def _slash_completer(commands: dict[str, str], arguments: dict[str, Callable[[], list[str]]]) -> Any:
    """Completes `/command` names (with a one-line description shown beside
    each) and, after a command, its arguments."""
    from prompt_toolkit.completion import Completer, Completion

    class SlashCompleter(Completer):
        def get_completions(self, document: Any, complete_event: Any) -> Any:
            text = document.text_before_cursor
            if "\n" in text or not text.startswith("/"):
                return
            if " " not in text:
                for name, description in commands.items():
                    if name.startswith(text):
                        yield Completion(name, start_position=-len(text), display_meta=description)
                return
            name, _, typed = text.partition(" ")
            provider = arguments.get(name)
            if provider is None:
                return
            try:
                values = provider()
            except Exception:  # noqa: BLE001 - a broken provider just offers nothing
                return
            for value in values:
                if value.startswith(typed):
                    yield Completion(value, start_position=-len(typed))

    return SlashCompleter()


def make_input(
    commands: dict[str, str] | list[str],
    history_file: str,
    status: Callable[[], str],
    on_cycle_mode: Callable[[], None] | None = None,
    arguments: dict[str, Callable[[], list[str]]] | None = None,
) -> Any:
    """A prompt session, or None when stdin/stdout aren't a terminal (callers
    then fall back to plain line input). `commands` maps each slash command to a
    short description; `arguments` maps a command to a function returning the
    values to offer after it. `on_cycle_mode` runs on Shift+Tab; the toolbar is
    redrawn afterward so the new mode shows at once."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.styles import Style
    except ImportError:
        return None

    os.makedirs(os.path.dirname(history_file), exist_ok=True)

    keys = KeyBindings()

    @keys.add("enter")
    def _send(event: Any) -> None:
        event.current_buffer.validate_and_handle()

    @keys.add("escape", "enter")
    @keys.add("c-j")
    def _newline(event: Any) -> None:
        event.current_buffer.insert_text("\n")

    if on_cycle_mode is not None:

        @keys.add("s-tab")
        def _cycle(event: Any) -> None:
            on_cycle_mode()
            event.app.invalidate()

    _map_shift_enter()

    def toolbar() -> list[tuple[str, str]]:
        # The first line is the bottom border of the input box.
        return [
            ("class:rule", _rule() + "\n"),
            ("class:toolbar.value", f" {status()}\n"),
            ("class:toolbar.hint", f" {KEY_HINTS}"),
        ]

    return PromptSession(
        history=FileHistory(history_file),
        completer=_slash_completer(
            commands if isinstance(commands, dict) else {name: "" for name in commands},
            arguments or {},
        ),
        complete_while_typing=True,
        multiline=True,
        key_bindings=keys,
        bottom_toolbar=toolbar,
        prompt_continuation=lambda width, line_number, wrap_count: " " * width,
        style=Style.from_dict(PROMPT_STYLES),
        erase_when_done=True,
    )


def read_input(session: Any, prompt: str = "") -> str:
    """Read one submission. With a session: the framed multiline box, then the
    submitted text is redrawn as a user block. Without: a plain line. Raises
    EOFError (Ctrl+D) / KeyboardInterrupt (Ctrl+C) like input()."""
    if session is None:
        return input(prompt)
    label = prompt.rstrip("> ").rstrip()
    message = [("class:rule", _rule() + "\n")]  # top border
    if label:
        message.append(("class:hint", label + " "))
    message.append(("class:prompt", PROMPT_MARKER))
    text = session.prompt(message)
    if text.strip():
        render_user(text.strip())
    return text
