"""Save and resume the REPL conversation across restarts.

History is stored per project directory under `~/.forge/sessions/`, so it
never lands inside the project (and never shows up as a git change).
Everything is best-effort: a failed read or write is ignored, since losing
a saved session must never break a task. An empty path disables it.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

SESSIONS_DIR = os.path.join(os.path.expanduser("~"), ".forge", "sessions")


def default_path(working_dir: str) -> str:
    """One session file per project directory."""
    absolute = os.path.abspath(working_dir)
    digest = hashlib.sha1(absolute.encode("utf-8")).hexdigest()[:12]
    name = os.path.basename(absolute) or "root"
    return os.path.join(SESSIONS_DIR, f"{name}-{digest}.json")


def _to_jsonable(obj: Any) -> Any:
    # The ollama client can hand back pydantic objects (e.g. tool calls).
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    return str(obj)


def save(path: str, history: list[dict[str, Any]]) -> None:
    if not path:
        return
    tmp = f"{path}.tmp"
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(history, f, default=_to_jsonable)
        os.replace(tmp, path)  # atomic: a crash never leaves a half-written file
    except (OSError, TypeError, ValueError):
        pass


def load(path: str) -> list[dict[str, Any]] | None:
    """The saved history, or None if there's none or it's unusable."""
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, list) or not data:
        return None
    if not all(isinstance(m, dict) and "role" in m for m in data):
        return None
    return data


def clear(path: str) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass
