"""Custom tools without editing `forge/tools.py`.

Drop a Python file in `~/.forge/plugins/` (yours, always loaded) or
`<project>/.forge/plugins/` (loaded after you approve it: it's code that
runs inside forge). Each file registers tools with the `@tool` decorator:

    from forge.plugins import tool

    @tool(
        description="Count the lines in a file.",
        parameters={"path": {"type": "string", "description": "File to count."}},
        read_only=True,
    )
    def count_lines(base_dir, path):
        with open(os.path.join(base_dir, path)) as f:
            return f"{sum(1 for _ in f)} lines"

The function receives the project directory first, then the arguments the
model chose, and returns text. Anything it raises comes back to the model
as an "Error: ..." result instead of crashing forge. Tools are offered in
plan mode only if declared `read_only=True`. Plugins are trusted code:
forge doesn't sandbox or time-limit them.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

MAX_RESULT_CHARS = 20_000
USER_PLUGIN_DIR = os.path.join(os.path.expanduser("~"), ".forge", "plugins")
PROJECT_PLUGIN_SUBDIR = os.path.join(".forge", "plugins")
TRUST_FILE = os.path.join(os.path.expanduser("~"), ".forge", "plugin_trust.json")

_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class PluginError(Exception):
    """A plugin declared something invalid."""


@dataclass
class PluginTool:
    name: str
    description: str
    schema: dict[str, Any]
    func: Callable[..., Any]
    read_only: bool
    source: str  # the plugin file


# Filled by @tool while a plugin file is being executed by the loader.
_collecting: list[PluginTool] | None = None


def tool(
    description: str,
    parameters: dict[str, dict[str, Any]] | None = None,
    required: list[str] | None = None,
    read_only: bool = False,
    name: str | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Register the decorated function as a model-callable tool.

    `parameters` maps each argument name to its JSON-schema property;
    `required` defaults to all of them. `read_only=True` also offers the
    tool in plan mode. `name` defaults to the function's name.
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name or func.__name__
        if not _NAME.match(tool_name):
            raise PluginError(f"invalid tool name {tool_name!r} (letters, digits, '_'; max 64)")
        if not isinstance(description, str) or not description.strip():
            raise PluginError(f"tool {tool_name!r} needs a description")
        props = parameters or {}
        if not isinstance(props, dict) or not all(isinstance(v, dict) for v in props.values()):
            raise PluginError(f"tool {tool_name!r}: parameters must map names to schema dicts")
        needed = list(props) if required is None else list(required)
        unknown = [r for r in needed if r not in props]
        if unknown:
            raise PluginError(f"tool {tool_name!r}: required names not in parameters: {unknown}")

        if _collecting is not None:
            _collecting.append(
                PluginTool(
                    name=tool_name,
                    description=description.strip(),
                    schema={"type": "object", "properties": props, "required": needed},
                    func=func,
                    read_only=bool(read_only),
                    source="",
                )
            )
        return func

    return decorator


# ---------------------------------------------------------------------------
# Trust (project plugins only; ~/.forge/plugins is the user's own)
# ---------------------------------------------------------------------------


def plugin_files(directory: str) -> list[str]:
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    return [
        os.path.join(directory, n)
        for n in names
        if n.endswith(".py") and not n.startswith("_") and os.path.isfile(os.path.join(directory, n))
    ]


def fingerprint(directory: str) -> str:
    """Hash of every plugin file's name and contents."""
    digest = hashlib.sha256()
    for path in plugin_files(directory):
        digest.update(os.path.basename(path).encode("utf-8"))
        try:
            with open(path, "rb") as f:
                digest.update(hashlib.sha256(f.read()).digest())
        except OSError:
            digest.update(b"unreadable")
    return digest.hexdigest()


def _load_trust() -> dict[str, str]:
    try:
        with open(TRUST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_trusted(project_dir: str, directory: str) -> bool:
    return _load_trust().get(os.path.realpath(project_dir)) == fingerprint(directory)


def trust(project_dir: str, directory: str) -> None:
    data = _load_trust()
    data[os.path.realpath(project_dir)] = fingerprint(directory)
    try:
        os.makedirs(os.path.dirname(TRUST_FILE), exist_ok=True)
        with open(TRUST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class PluginRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, PluginTool] = {}
        self.errors: dict[str, str] = {}  # file -> what went wrong

    def load(self, directory: str, reserved: set[str]) -> None:
        """Import every plugin file in `directory`. A broken file is recorded
        and skipped; it never stops the others or forge itself."""
        global _collecting
        for path in plugin_files(directory):
            module_name = "forge_plugin_" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:10]
            _collecting = []
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise PluginError("could not load file")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                found = _collecting
            except Exception as e:  # noqa: BLE001 - plugin code is untrusted to be correct
                self.errors[path] = f"{type(e).__name__}: {e}"
                continue
            finally:
                _collecting = None

            for entry in found:
                entry.source = path
                if entry.name in reserved or entry.name.startswith("mcp__"):
                    self.errors[path] = f"tool name {entry.name!r} is reserved by forge"
                elif entry.name in self.tools:
                    self.errors[path] = (
                        f"tool name {entry.name!r} is already defined by {self.tools[entry.name].source}"
                    )
                else:
                    self.tools[entry.name] = entry

    def has_tool(self, name: str) -> bool:
        return name in self.tools

    def is_read_only(self, name: str) -> bool:
        entry = self.tools.get(name)
        return bool(entry and entry.read_only)

    def schemas(self, read_only: bool = False) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description[:1000],
                    "parameters": t.schema,
                },
            }
            for t in self.tools.values()
            if t.read_only or not read_only
        ]

    def call(self, name: str, base_dir: str, arguments: dict[str, Any]) -> str:
        """Run a plugin tool. Never raises: failures become "Error: ..." text."""
        entry = self.tools[name]
        try:
            result = entry.func(base_dir, **arguments)
        except TypeError as e:
            return f"Error: invalid arguments for {name!r}: {e}"
        except Exception as e:  # noqa: BLE001 - plugin code must not crash forge
            return f"Error: {type(e).__name__}: {e}"

        text = result if isinstance(result, str) else str(result)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n... [truncated, {len(text) - MAX_RESULT_CHARS} more characters]"
        return text or "(no output)"

    def describe(self) -> str:
        if not self.tools and not self.errors:
            return "No plugins loaded."
        lines = []
        for entry in self.tools.values():
            flag = " [read-only]" if entry.read_only else ""
            lines.append(f"{entry.name}{flag} - {os.path.basename(entry.source)}: {entry.description}")
        lines.extend(f"{os.path.basename(path)}: failed to load: {error}" for path, error in self.errors.items())
        return "\n".join(lines)


_registry: PluginRegistry | None = None


def set_registry(registry: PluginRegistry | None) -> None:
    global _registry
    _registry = registry


def get_registry() -> PluginRegistry | None:
    return _registry
