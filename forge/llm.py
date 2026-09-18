"""The ReAct-style tool-calling loop that drives an agent task.

`run_task()` sends the conversation to Ollama, executes any tool calls the
model asks for via `forge.tools.call_tool`, feeds the results back, and
repeats until the model stops calling tools (or `max_iterations` is hit).

Deliberately has no baked-in persona: `new_history()` returns `[]` — no
system message. The model only ever sees the tool schemas and the task.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .tools import call_tool, tool_schemas_for


class LLMError(Exception):
    """Raised when a task attempt can't continue at all (e.g. the model
    backend is unreachable, or returned a response with no usable
    content). Callers should catch this, report it, and let the REPL
    keep running rather than letting it propagate and kill the process.
    """


class OllamaUnreachableError(LLMError):
    """The model backend couldn't be reached (server down, wrong host, or
    the connection dropped mid-response). Kept separate from `LLMError` so
    callers, which know the configured host, can give a clear message.
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


def _record_usage(response: Any, usage_file: str | None = None) -> None:
    prompt = response.get("prompt_eval_count") or 0
    completion = response.get("eval_count") or 0
    with _usage_lock:
        _usage["prompt_tokens"] += prompt
        _usage["completion_tokens"] += completion
        _usage["calls"] += 1
    if usage_file:
        _add_to_persisted_usage(usage_file, prompt, completion)


# ---------------------------------------------------------------------------
# Persisted (cross-process, cross-session) usage
# ---------------------------------------------------------------------------
#
# `_usage` above only lives for one process. Since a one-shot `forge
# "task"` invocation is a whole new process every time, a *running total*
# has to be file-backed. This is best-effort: a failure to read or write
# the file never raises — it just means the running total doesn't grow
# for that call — since token accounting must never be allowed to break
# an actual task.

_EMPTY_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}


def _load_persisted_usage(path: str) -> dict[str, int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return dict(_EMPTY_USAGE)

    if not isinstance(data, dict):
        return dict(_EMPTY_USAGE)

    return {key: int(data.get(key, 0) or 0) for key in _EMPTY_USAGE}


def _save_persisted_usage(path: str, usage: dict[str, int]) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(usage, f)
    except OSError:
        pass


def get_persisted_usage(path: str) -> dict[str, int]:
    return _load_persisted_usage(path)


def reset_persisted_usage(path: str) -> None:
    _save_persisted_usage(path, dict(_EMPTY_USAGE))


def _add_to_persisted_usage(path: str, prompt: int, completion: int) -> None:
    usage = _load_persisted_usage(path)
    usage["prompt_tokens"] += prompt
    usage["completion_tokens"] += completion
    usage["calls"] += 1
    _save_persisted_usage(path, usage)


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


def _stream_chat(
    client: Any,
    model: str,
    messages: list[dict[str, Any]],
    tools: Any,
    on_token: Callable[[str], None],
) -> dict[str, Any]:
    """Call `client.chat(stream=True)`, forwarding each content chunk to
    `on_token` as it arrives, and fold the chunks back into the same
    response shape a non-streaming call returns so the loop below doesn't
    care which path produced it.
    """
    content_parts: list[str] = []
    tool_calls: list[Any] = []
    prompt_tokens = 0
    completion_tokens = 0

    for chunk in client.chat(model=model, messages=messages, tools=tools, stream=True):
        message = chunk.get("message") or {}
        piece = message.get("content") or ""
        if piece:
            content_parts.append(piece)
            on_token(piece)
        tool_calls.extend(message.get("tool_calls") or [])
        # Token counts only arrive on the final (done) chunk.
        prompt_tokens = chunk.get("prompt_eval_count") or prompt_tokens
        completion_tokens = chunk.get("eval_count") or completion_tokens

    return {
        "message": {"content": "".join(content_parts), "tool_calls": tool_calls},
        "prompt_eval_count": prompt_tokens,
        "eval_count": completion_tokens,
    }


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
    usage_file: str | None = None,
    read_only: bool = False,
    on_token: Callable[[str], None] | None = None,
) -> TaskResult:
    """Run one task to completion against `client` (an object exposing a
    `.chat(model=, messages=, tools=)` method — an `ollama.Client` in
    production, a stub/mock in tests).

    `read_only=True` is plan mode: the model is only offered read-only
    tools (`read_file`, `list_dir`) and is told to produce a plan instead
    of acting. `call_tool` also refuses write/shell tools defensively, in
    case the model calls one anyway despite it not being offered.

    If `on_token` is given, the response is streamed and each content chunk
    is passed to it as it arrives; otherwise the call blocks for the full
    response, as before.
    """
    messages = list(history)
    task_content = (
        f"[Plan mode: read-only. Investigate and describe a plan; do not "
        f"attempt to modify files or run commands — those tools are "
        f"unavailable right now.]\n\n{task}"
        if read_only
        else task
    )
    messages.append({"role": "user", "content": task_content})

    tools = tool_schemas_for(read_only)
    last_content = ""

    for iteration in range(max_iterations):
        try:
            if on_token is not None:
                response = _stream_chat(client, model, messages, tools, on_token)
            else:
                response = client.chat(model=model, messages=messages, tools=tools)
        except (ConnectionError, httpx.TransportError) as e:
            raise OllamaUnreachableError(f"{type(e).__name__}: {e}") from e
        except Exception as e:
            raise LLMError(f"Model backend call failed: {type(e).__name__}: {e}") from e

        if response is None or "message" not in response:
            raise LLMError("Model backend returned a response with no 'message' field")

        _record_usage(response, usage_file)

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

        # Streamed text from this step ends here; break the line so the next
        # step's text doesn't run into it.
        if on_token is not None and content:
            on_token("\n")

        for raw_call in tool_calls:
            name, raw_args, error = _extract_tool_call(raw_call)
            if error is not None:
                result = f"Error: {error}"
            else:
                arguments, parse_error = _parse_tool_arguments(raw_args)
                if parse_error is not None:
                    result = f"Error: {parse_error}"
                else:
                    result = call_tool(
                        name, arguments, base_dir, shell_timeout=shell_timeout, read_only=read_only
                    )

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
