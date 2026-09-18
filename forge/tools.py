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

from . import lsp, mcp, plugins, testrunner, undo


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


def _read_bytes(target: str) -> bytes | None:
    """The file's current bytes, or None if there's no such file."""
    if not os.path.isfile(target):
        return None
    with open(target, "rb") as f:
        return f.read()


def write_file(base_dir: str, path: str, content: str) -> str:
    target = _safe_path(base_dir, path)
    before = _read_bytes(target)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write(content)
    undo.record(target, before, _read_bytes(target), scope=os.path.realpath(base_dir))
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
    before = _read_bytes(target)
    with open(target, "w", encoding="utf-8") as f:
        f.write(new_content)
    undo.record(target, before, _read_bytes(target), scope=os.path.realpath(base_dir))
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


def run_tests(
    base_dir: str,
    path: str | None = None,
    command: str | None = None,
    verbose: Any = False,
    timeout: int = testrunner.DEFAULT_TIMEOUT,
) -> str:
    if path:
        if path.startswith("-"):
            raise ToolError("path must be a test file, directory or test id, not an option")
        _safe_path(base_dir, path.split("::", 1)[0])  # must stay inside the project
    if isinstance(verbose, str):
        verbose = verbose.strip().lower() in ("1", "true", "yes")
    return testrunner.run_tests(base_dir, path, command, bool(verbose), timeout)


# ---------------------------------------------------------------------------
# Schemas + dispatcher
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Language-server tools (semantic code intelligence)
# ---------------------------------------------------------------------------

_MAX_LOCATIONS = 50
_MAX_DIAGNOSTICS = 100


def _lsp_target(base_dir: str, path: str) -> str:
    target = _safe_path(base_dir, path)
    if not os.path.isfile(target):
        raise ToolError(f"Not a file: {path!r}")
    if os.path.splitext(target)[1] not in lsp.PYTHON_EXTENSIONS:
        raise ToolError("Only Python (.py/.pyi) files are supported by the language server")
    return target


def _position_arg(value: Any, name: str) -> int:
    """Models often send numbers as strings; accept those, reject the rest."""
    if isinstance(value, bool):
        raise ToolError(f"{name} must be a positive integer")
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a positive integer, got {value!r}")
    if number < 1:
        raise ToolError(f"{name} must be 1 or greater (positions are 1-based)")
    return number


def _client(base_dir: str) -> lsp.LSPClient:
    try:
        return lsp.get_client(base_dir)
    except lsp.LSPError as e:
        raise ToolError(str(e))


def _display_path(base_dir: str, path: str) -> str:
    base_real = os.path.realpath(base_dir)
    try:
        if os.path.commonpath([base_real, path]) == base_real:
            return os.path.relpath(path, base_real)
    except ValueError:
        pass
    return path


def _source_line(path: str, line: int) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            for number, text in enumerate(f, start=1):
                if number == line:
                    return text.strip()[:120]
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _format_locations(base_dir: str, locations: list[dict[str, Any]], empty: str) -> str:
    if not locations:
        return empty
    lines = []
    for loc in locations[:_MAX_LOCATIONS]:
        path = lsp.uri_to_path(loc["uri"])
        snippet = _source_line(path, loc["line"])
        where = f"{_display_path(base_dir, path)}:{loc['line']}:{loc['column']}"
        lines.append(f"{where}  {snippet}".rstrip())
    if len(locations) > _MAX_LOCATIONS:
        lines.append(f"... and {len(locations) - _MAX_LOCATIONS} more")
    return "\n".join(lines)


def find_definition(base_dir: str, path: str, line: Any, column: Any) -> str:
    target = _lsp_target(base_dir, path)
    line, column = _position_arg(line, "line"), _position_arg(column, "column")
    try:
        locations = _client(base_dir).definition(target, line, column)
    except lsp.LSPError as e:
        raise ToolError(str(e))
    return _format_locations(base_dir, locations, "No definition found at that position")


def find_references(base_dir: str, path: str, line: Any, column: Any) -> str:
    target = _lsp_target(base_dir, path)
    line, column = _position_arg(line, "line"), _position_arg(column, "column")
    try:
        locations = _client(base_dir).references(target, line, column)
    except lsp.LSPError as e:
        raise ToolError(str(e))
    return _format_locations(base_dir, locations, "No references found at that position")


def hover(base_dir: str, path: str, line: Any, column: Any) -> str:
    target = _lsp_target(base_dir, path)
    line, column = _position_arg(line, "line"), _position_arg(column, "column")
    try:
        text = _client(base_dir).hover(target, line, column)
    except lsp.LSPError as e:
        raise ToolError(str(e))
    return text or "No hover information at that position"


def get_diagnostics(base_dir: str, path: str) -> str:
    target = _lsp_target(base_dir, path)
    try:
        diagnostics = _client(base_dir).diagnostics(target)
    except lsp.LSPError as e:
        raise ToolError(str(e))
    if not diagnostics:
        return "No diagnostics: no errors or warnings"

    diagnostics = sorted(
        diagnostics,
        key=lambda d: (d["range"]["start"]["line"], d["range"]["start"]["character"]),
    )
    shown = _display_path(base_dir, target)
    lines = []
    for d in diagnostics[:_MAX_DIAGNOSTICS]:
        start = d["range"]["start"]
        severity = lsp.SEVERITIES.get(d.get("severity", 1), "error")
        code = f" [{d['code']}]" if d.get("code") else ""
        lines.append(
            f"{shown}:{start['line'] + 1}:{start['character'] + 1} {severity}: {d['message']}{code}"
        )
    if len(diagnostics) > _MAX_DIAGNOSTICS:
        lines.append(f"... and {len(diagnostics) - _MAX_DIAGNOSTICS} more")
    return "\n".join(lines)


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
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the project's tests (pytest or unittest, auto-detected) and get a short summary: "
                "pass/fail counts and the names of failing tests. Prefer this over run_shell for testing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional test file, directory or test id (e.g. tests/test_a.py::test_x)."},
                    "command": {"type": "string", "description": "Optional custom test command (e.g. 'npm test') instead of auto-detection."},
                    "verbose": {"type": "boolean", "description": "Include the full test output."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_definition",
            "description": "Go to the definition of the symbol at a position in a Python file (semantic, via the language server).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Python file path, relative to the working directory."},
                    "line": {"type": "integer", "description": "1-based line number."},
                    "column": {"type": "integer", "description": "1-based column number."},
                },
                "required": ["path", "line", "column"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_references",
            "description": "Find every reference to the symbol at a position in a Python file (semantic, via the language server).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Python file path, relative to the working directory."},
                    "line": {"type": "integer", "description": "1-based line number."},
                    "column": {"type": "integer", "description": "1-based column number."},
                },
                "required": ["path", "line", "column"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hover",
            "description": "Get the type and docstring of the symbol at a position in a Python file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Python file path, relative to the working directory."},
                    "line": {"type": "integer", "description": "1-based line number."},
                    "column": {"type": "integer", "description": "1-based column number."},
                },
                "required": ["path", "line", "column"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_diagnostics",
            "description": "Type-check a Python file and list its errors and warnings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Python file path, relative to the working directory."},
                },
                "required": ["path"],
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
    "run_tests": run_tests,
    "find_definition": find_definition,
    "find_references": find_references,
    "hover": hover,
    "get_diagnostics": get_diagnostics,
}

# Tools that only inspect the repo, never change it or run arbitrary code.
# Plan mode restricts the model to this set.
READ_ONLY_TOOLS = {
    "read_file",
    "list_dir",
    "find_definition",
    "find_references",
    "hover",
    "get_diagnostics",
}


def tool_schemas_for(read_only: bool) -> list[dict[str, Any]]:
    """The tool schemas to hand the model: all of them normally, or just
    the read-only ones in plan mode.
    """
    builtin = (
        TOOL_SCHEMAS
        if not read_only
        else [s for s in TOOL_SCHEMAS if s["function"]["name"] in READ_ONLY_TOOLS]
    )
    manager = mcp.get_manager()
    registry = plugins.get_registry()
    extra = (manager.schemas(read_only) if manager else []) + (
        registry.schemas(read_only) if registry else []
    )
    return builtin + extra if extra else builtin


def call_tool(
    name: str,
    arguments: dict[str, Any],
    base_dir: str,
    shell_timeout: int = 60,
    read_only: bool = False,
) -> str:
    """Execute a tool by name and return a string result.

    This function must never raise. Any failure — expected (`ToolError`,
    bad argument types) or unexpected (permission errors, disk-full,
    broken symlinks, subprocess failures, anything) — is converted into an
    `"Error: ..."` string so the caller can feed it straight back to the
    model as a tool result instead of crashing the agent loop.

    `read_only=True` (plan mode) is enforced here too, not just by
    omitting the schema from what's offered to the model — a model can
    still name a tool that wasn't in its schema list.
    """
    if read_only and name not in READ_ONLY_TOOLS and name in _DISPATCH:
        return f"Error: {name!r} is not allowed in plan mode (read-only). Exit plan mode with /build to make changes."

    manager = mcp.get_manager()
    if manager is not None and manager.has_tool(name):
        if read_only and not manager.is_read_only(name):
            return f"Error: {name!r} is not allowed in plan mode (it isn't marked read-only). Exit plan mode with /build to use it."
        try:
            return manager.call(name, dict(arguments))
        except mcp.MCPError as e:
            return f"Error: {e}"
        except Exception as e:  # noqa: BLE001 - same never-raise contract as below
            return f"Error: {type(e).__name__}: {e}"

    registry = plugins.get_registry()
    if registry is not None and registry.has_tool(name):
        if read_only and not registry.is_read_only(name):
            return f"Error: {name!r} is not allowed in plan mode (the plugin doesn't mark it read-only). Exit plan mode with /build to use it."
        return registry.call(name, base_dir, dict(arguments))

    func = _DISPATCH.get(name)
    if func is None:
        return f"Error: unknown tool {name!r}"

    kwargs = dict(arguments)
    if name == "run_shell":
        kwargs.setdefault("timeout", shell_timeout)
    elif name == "run_tests":
        # A test suite legitimately takes longer than a one-off command.
        kwargs.setdefault("timeout", max(shell_timeout, testrunner.DEFAULT_TIMEOUT))

    try:
        return func(base_dir, **kwargs)
    except ToolError as e:
        return f"Error: {e}"
    except TypeError as e:
        return f"Error: invalid arguments for {name!r}: {e}"
    except Exception as e:  # noqa: BLE001 - intentional catch-all, see docstring
        return f"Error: {type(e).__name__}: {e}"
