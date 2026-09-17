"""The ReAct-style tool-calling loop that drives an agent task.

`run_task()` sends the conversation to Ollama, executes any tool calls the
model asks for via `forge.tools.call_tool`, feeds the results back, and
repeats until the model stops calling tools (or `max_iterations` is hit).

Deliberately has no baked-in persona: `new_history()` returns `[]` — no
system message. The model only ever sees the tool schemas and the task.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

from .tools import TOOL_SCHEMAS, call_tool


class LLMError(Exception):
    """Raised when a task attempt can't continue at all (e.g. the model
    backend is unreachable, or returned a response with no usable
    content). Callers should catch this, report it, and let the REPL
    keep running rather than letting it propagate and kill the process.
    """


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------
#
# forge is a single-threaded, single-process CLI/REPL: at most one
# `run_task()` call is ever in flight at a time, driven from one REPL input
# loop. A module-level counter is safe under that usage pattern. It is
# *not* safe if forge were ever driven concurrently (e.g. a future
# multi-session server mode) — the lock below makes increments atomic so a
# future concurrent caller doesn't corrupt counts, but callers still need
# to reason about interleaved *conversations* separately; this only
# protects the counter itself.

_usage_lock = threading.Lock()
_usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


def get_usage() -> dict[str, int]:
    with _usage_lock:
        return dict(_usage)


def reset_usage() -> None:
    with _usage_lock:
        _usage["prompt_tokens"] = 0
        _usage["completion_tokens"] = 0
        _usage["calls"] = 0


def _record_usage(response: Any) -> None:
    prompt = response.get("prompt_eval_count") or 0
    completion = response.get("eval_count") or 0
    with _usage_lock:
        _usage["prompt_tokens"] += prompt
        _usage["completion_tokens"] += completion
        _usage["calls"] += 1


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def new_history() -> list[dict[str, Any]]:
    """A fresh conversation: no system message, no persona."""
    return []


# ---------------------------------------------------------------------------
# Tool-call argument parsing
# ---------------------------------------------------------------------------


def _parse_tool_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """Parse a tool call's arguments, whatever shape the model sent them in.

    Returns (arguments, error). Never raises: a small local model can
    return malformed or truncated JSON, and that must come back as an
    error the model can see and retry from, not a crash.
    """
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        if not raw.strip():
            return {}, None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return None, f"Could not parse tool arguments as JSON: {e}"
        if not isinstance(parsed, dict):
            return None, "Tool arguments must be a JSON object"
        return parsed, None
    return None, f"Unsupported tool arguments type: {type(raw).__name__}"


def _extract_tool_call(raw_call: Any) -> tuple[str | None, Any, str | None]:
    """Pull (name, raw_arguments, error) out of one tool_calls entry,
    tolerating a model that omits keys it's supposed to send.
    """
    if not isinstance(raw_call, dict):
        return None, None, f"Malformed tool call (not an object): {raw_call!r}"

    function = raw_call.get("function")
    if not isinstance(function, dict):
        return None, None, "Malformed tool call: missing 'function'"

    name = function.get("name")
    if not name:
        return None, None, "Malformed tool call: missing 'function.name'"

    return name, function.get("arguments"), None


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


@dataclass
class TaskResult:
    content: str
    history: list[dict[str, Any]] = field(default_factory=list)
    iterations: int = 0


def run_task(
    task: str,
    history: list[dict[str, Any]],
    client: Any,
    model: str,
    base_dir: str = ".",
    shell_timeout: int = 60,
    max_iterations: int = 25,
) -> TaskResult:
    """Run one task to completion against `client` (an object exposing a
    `.chat(model=, messages=, tools=)` method — an `ollama.Client` in
    production, a stub/mock in tests).
    """
    messages = list(history)
    messages.append({"role": "user", "content": task})

    last_content = ""

    for iteration in range(max_iterations):
        try:
            response = client.chat(model=model, messages=messages, tools=TOOL_SCHEMAS)
        except Exception as e:
            raise LLMError(f"Model backend call failed: {type(e).__name__}: {e}") from e

        if response is None or "message" not in response:
            raise LLMError("Model backend returned a response with no 'message' field")

        _record_usage(response)

        message = response["message"]
        content = message.get("content") or ""
        last_content = content
        tool_calls = message.get("tool_calls") or []

        messages.append(
            {
                "role": "assistant",
                "content": content,
                **({"tool_calls": tool_calls} if tool_calls else {}),
            }
        )

        if not tool_calls:
            return TaskResult(content=last_content, history=messages, iterations=iteration + 1)

        for raw_call in tool_calls:
            name, raw_args, error = _extract_tool_call(raw_call)
            if error is not None:
                result = f"Error: {error}"
            else:
                arguments, parse_error = _parse_tool_arguments(raw_args)
                if parse_error is not None:
                    result = f"Error: {parse_error}"
                else:
                    result = call_tool(name, arguments, base_dir, shell_timeout=shell_timeout)

            messages.append(
                {
                    "role": "tool",
                    "content": result,
                    **({"name": name} if name else {}),
                }
            )

    return TaskResult(
        content=last_content or f"Stopped after {max_iterations} iterations without finishing.",
        history=messages,
        iterations=max_iterations,
    )
