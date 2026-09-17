"""File and shell tools available to the agent loop, plus the dispatcher
that turns a model-requested tool call into a string result.

Every public tool function here may raise. The single place that is
responsible for making sure nothing raised by a tool ever escapes into the
agent loop is `call_tool()` — see its docstring.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any


class ToolError(Exception):
    """A tool-level failure that should be reported back to the model."""


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def _safe_path(base_dir: str, path: str) -> str:
    """Resolve `path` relative to `base_dir` and guarantee the result is
    still inside `base_dir`.

    Resolves symlinks and `..` segments via `os.path.realpath` on *both*
    sides before comparing, so a symlink inside the sandbox that points
    outside it (or a `..` chain) can't be used to escape. Comparison is done
    with `os.path.commonpath` on the realpath'd, OS-normalized strings, so
    it behaves correctly on case-insensitive filesystems (e.g. default
    macOS/APFS) since both sides go through the same normalization.
    """
    base_real = os.path.realpath(base_dir)
    candidate = path if os.path.isabs(path) else os.path.join(base_dir, path)
    candidate_real = os.path.realpath(candidate)

    try:
        common = os.path.commonpath([base_real, candidate_real])
    except ValueError:
        # Different drives/roots entirely.
        raise ToolError(f"Path escapes working directory: {path!r}")

    if common != base_real:
        raise ToolError(f"Path escapes working directory: {path!r}")

    return candidate_real


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def read_file(base_dir: str, path: str) -> str:
    target = _safe_path(base_dir, path)
    if not os.path.isfile(target):
        raise ToolError(f"Not a file: {path!r}")
    with open(target, "r", encoding="utf-8") as f:
        return f.read()


def write_file(base_dir: str, path: str, content: str) -> str:
    target = _safe_path(base_dir, path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {len(content)} bytes to {path}"


def edit_file(base_dir: str, path: str, old_str: str, new_str: str) -> str:
    target = _safe_path(base_dir, path)
    if not os.path.isfile(target):
        raise ToolError(f"Not a file: {path!r}")

    with open(target, "r", encoding="utf-8") as f:
        content = f.read()

    count = content.count(old_str)
    if count == 0:
        raise ToolError(f"old_str not found in {path!r}")
    if count > 1:
        raise ToolError(
            f"old_str is not unique in {path!r} ({count} matches) — "
            "provide more surrounding context"
        )

    new_content = content.replace(old_str, new_str, 1)
    with open(target, "w", encoding="utf-8") as f:
        f.write(new_content)
    return f"Edited {path}"


def list_dir(base_dir: str, path: str = ".") -> str:
    target = _safe_path(base_dir, path)
    if not os.path.isdir(target):
        raise ToolError(f"Not a directory: {path!r}")

    lines: list[str] = []

    def _on_error(err: OSError) -> None:
        lines.append(f"<error reading {err.filename!r}: {err.strerror}>")

    for root, dirs, files in os.walk(target, onerror=_on_error):
        rel_root = os.path.relpath(root, target)
        for name in sorted(dirs) + sorted(files):
            rel_path = name if rel_root == "." else os.path.join(rel_root, name)
            entry = os.path.join(root, name)
            try:
                is_dir = os.path.isdir(entry)
                lines.append(rel_path + "/" if is_dir else rel_path)
            except OSError as e:
                lines.append(f"{rel_path} <error: {e.strerror}>")

    return "\n".join(sorted(lines)) if lines else "<empty>"


def run_shell(base_dir: str, command: str, timeout: int = 60) -> str:
    result = subprocess.run(
        command,
        shell=True,
        cwd=base_dir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    parts = [f"exit code: {result.returncode}"]
    if result.stdout:
        parts.append(f"stdout:\n{result.stdout}")
    if result.stderr:
        parts.append(f"stderr:\n{result.stderr}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Schemas + dispatcher
# ---------------------------------------------------------------------------

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, relative to the working directory."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Write content to a file, overwriting it if it exists (creates parent directories as needed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, relative to the working directory."},
                    "content": {"type": "string", "description": "Full file contents to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one unique occurrence of old_str with new_str in a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path, relative to the working directory."},
                    "old_str": {"type": "string", "description": "Exact text to replace; must appear exactly once."},
                    "new_str": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Recursively list files and directories under a path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path, relative to the working directory. Defaults to '.'."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "Run a shell command in the working directory and return its output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                },
                "required": ["command"],
            },
        },
    },
]

_DISPATCH = {
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "run_shell": run_shell,
}


def call_tool(name: str, arguments: dict[str, Any], base_dir: str, shell_timeout: int = 60) -> str:
    """Execute a tool by name and return a string result.

    This function must never raise. Any failure — expected (`ToolError`,
    bad argument types) or unexpected (permission errors, disk-full,
    broken symlinks, subprocess failures, anything) — is converted into an
    `"Error: ..."` string so the caller can feed it straight back to the
    model as a tool result instead of crashing the agent loop.
    """
    func = _DISPATCH.get(name)
    if func is None:
        return f"Error: unknown tool {name!r}"

    kwargs = dict(arguments)
    if name == "run_shell":
        kwargs.setdefault("timeout", shell_timeout)

    try:
        return func(base_dir, **kwargs)
    except ToolError as e:
        return f"Error: {e}"
    except TypeError as e:
        return f"Error: invalid arguments for {name!r}: {e}"
    except Exception as e:  # noqa: BLE001 - intentional catch-all, see docstring
        return f"Error: {type(e).__name__}: {e}"
