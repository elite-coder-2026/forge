"""Permission modes: what forge asks before it edits files or runs commands.

    default    ask before edits and before shell commands
    auto       edits go through; still ask before shell commands
    plan       read-only tools only (the existing plan mode)
    dangerous  never ask

`plan` is a mode of its own in the UI, but it is carried by `plan_mode` (as
before); the other three are the "edit mode". Answering "always" to a prompt
remembers that category (edits or shell) for the rest of the session.
"""

from __future__ import annotations

import difflib
from typing import Any, Callable

MODES = ("default", "auto", "plan", "dangerous")
EDIT_MODES = ("default", "auto", "dangerous")

EDIT_TOOLS = {"write_file", "edit_file"}
SHELL_TOOLS = {"run_shell", "run_tests"}

# What a prompt can answer.
ONCE, ALWAYS, DENY = "once", "always", "deny"


def label(plan_mode: bool, edit_mode: str) -> str:
    if plan_mode:
        return "plan"
    return {"default": "ask", "auto": "auto-edits", "dangerous": "dangerous"}.get(edit_mode, edit_mode)


def category(name: str) -> str | None:
    """'edits', 'shell', or None for tools that never need approval."""
    if name in EDIT_TOOLS:
        return "edits"
    if name in SHELL_TOOLS:
        return "shell"
    return None


class Approver:
    """Decides whether a tool call may run, asking `ask(name, arguments)`
    (which returns ONCE, ALWAYS or DENY) when the mode requires it.

    `always` is shared with the caller so "always" lasts for the session.
    """

    def __init__(
        self,
        edit_mode: str,
        ask: Callable[[str, dict[str, Any]], str],
        always: set[str] | None = None,
    ) -> None:
        self.edit_mode = edit_mode
        self._ask = ask
        self.always = always if always is not None else set()

    def __call__(self, name: str, arguments: dict[str, Any]) -> bool:
        kind = category(name)
        if kind is None or self.edit_mode == "dangerous":
            return True
        if kind == "edits" and self.edit_mode == "auto":
            return True
        if kind in self.always:
            return True
        answer = self._ask(name, arguments)
        if answer == ALWAYS:
            self.always.add(kind)
            return True
        return answer == ONCE


def _diff(old: str, new: str) -> str:
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=2)
    return "\n".join(line for line in lines if not line.startswith(("---", "+++")))


def describe(name: str, arguments: dict[str, Any]) -> tuple[str, str, str, bool]:
    """(title, target, body, body_is_diff) for an approval prompt."""
    if name == "edit_file":
        diff = _diff(str(arguments.get("old_str", "")), str(arguments.get("new_str", "")))
        return "Edit file", str(arguments.get("path", "?")), diff, True
    if name == "write_file":
        content = str(arguments.get("content", ""))
        lines = content.splitlines()
        preview = "\n".join(lines[:15]) + (f"\n… {len(lines) - 15} more lines" if len(lines) > 15 else "")
        return "Write file", str(arguments.get("path", "?")), preview, False
    if name == "run_shell":
        return "Run command", str(arguments.get("command", "?")), "", False
    if name == "run_tests":
        return "Run tests", str(arguments.get("command") or arguments.get("path") or "the test suite"), "", False
    return name, str(arguments.get("path") or arguments.get("command") or ""), "", False
