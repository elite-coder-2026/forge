"""Runtime configuration for forge.

Precedence, highest first: CLI flags, environment variables, a
`forge.toml` in the project directory, then the built-in defaults.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from . import session


DEFAULT_MODEL = "qwen2.5-coder"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MAX_ITERATIONS = 25
DEFAULT_SHELL_TIMEOUT = 60
DEFAULT_VOICE_SECONDS = 30
# Soft session budgets (see forge/budget.py). 0 disables a limit.
DEFAULT_BUDGET_TOKENS = 200_000
DEFAULT_BUDGET_MINUTES = 15.0
# Used to describe attached images (see forge/vision.py). Gemma 3 supports image input.
DEFAULT_VISION_MODEL = "gemma3:12b"
DEFAULT_USAGE_FILE = os.path.join(os.path.expanduser("~"), ".forge", "usage.json")

CONFIG_FILENAME = "forge.toml"

# Reference price used by `/usage` to estimate money saved by running
# locally instead of paying for hosted inference of an equivalent
# open-weight model. NOT a frontier-model (GPT-4o/Claude/etc.) price —
# those would wildly overstate "savings" for a small local coder model.
# Sourced from Together AI's published Qwen2.5-Coder pricing (see
# https://pricepertoken.com/pricing-page/model/qwen-qwen-2.5-coder-32b-instruct
# and https://deepinfra.com/blog/qwen-api-pricing-2026-guide) as of the
# time this was written — check current provider pricing if you want an
# up-to-date number; it's fully overridable via env vars below.
DEFAULT_PROMPT_PRICE_PER_1M = 0.30
DEFAULT_COMPLETION_PRICE_PER_1M = 0.80


class ConfigError(Exception):
    """`forge.toml` exists but can't be used (bad TOML, unknown key, wrong type)."""


@dataclass
class MCPServerConfig:
    """One `[mcp_servers.<name>]` table: an MCP server forge launches over stdio."""

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    timeout: float = 60.0


# Keys allowed in forge.toml, with the type each must be. (The working
# directory isn't one: it's what locates the file in the first place.)
_FILE_KEYS: dict[str, tuple[type, ...]] = {
    "model": (str,),
    "host": (str,),
    "vision_model": (str,),
    "fast_model": (str,),
    "max_iterations": (int,),
    "shell_timeout": (int,),
    "voice_record": (str,),
    "voice_transcribe": (str,),
    "voice_seconds": (int,),
    "budget_tokens": (int,),
    "budget_minutes": (int, float),
    "usage_file": (str,),
    "session_file": (str,),
    "mcp_servers": (dict,),
    "prompt_price_per_1m": (int, float),
    "completion_price_per_1m": (int, float),
}


def load_project_file(working_dir: str) -> dict[str, Any]:
    """Read and validate `<working_dir>/forge.toml`; {} if there isn't one.

    A file that exists but is invalid raises `ConfigError` rather than
    being ignored, so a typo'd key doesn't silently do nothing.
    """
    path = os.path.join(working_dir, CONFIG_FILENAME)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise ConfigError(f"{path}: could not be read: {e}") from e
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from e

    unknown = sorted(set(data) - set(_FILE_KEYS))
    if unknown:
        raise ConfigError(
            f"{path}: unknown key(s): {', '.join(unknown)}. "
            f"Valid keys: {', '.join(_FILE_KEYS)}"
        )

    for key, value in data.items():
        allowed = _FILE_KEYS[key]
        # bool is an int subclass in Python; `max_iterations = true` is a mistake.
        if isinstance(value, bool) or not isinstance(value, allowed):
            names = " or ".join(t.__name__ for t in allowed)
            raise ConfigError(f"{path}: '{key}' must be {names}, got {value!r}")

    if "mcp_servers" in data:
        _parse_mcp_servers(path, data["mcp_servers"])
    return data


_SERVER_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_SERVER_KEYS = {"command", "args", "env", "timeout"}


def _parse_mcp_servers(path: str, raw: dict[str, Any]) -> dict[str, MCPServerConfig]:
    """Validate the `[mcp_servers.*]` tables and build their configs."""
    servers: dict[str, MCPServerConfig] = {}
    for name, table in raw.items():
        where = f"{path}: mcp_servers.{name}"
        if not _SERVER_NAME.match(name):
            raise ConfigError(f"{where}: server names use letters, digits, '_' and '-' (max 32)")
        if not isinstance(table, dict):
            raise ConfigError(f"{where} must be a table")
        unknown = sorted(set(table) - _SERVER_KEYS)
        if unknown:
            raise ConfigError(
                f"{where}: unknown key(s): {', '.join(unknown)}. Valid keys: {', '.join(sorted(_SERVER_KEYS))}"
            )

        command = table.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ConfigError(f"{where}: 'command' is required and must be a string")
        args = table.get("args", [])
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ConfigError(f"{where}: 'args' must be a list of strings")
        env = table.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()
        ):
            raise ConfigError(f"{where}: 'env' must be a table of strings")
        timeout = table.get("timeout", 60.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ConfigError(f"{where}: 'timeout' must be a positive number of seconds")

        servers[name] = MCPServerConfig(command, list(args), dict(env), float(timeout))
    return servers


def _resolve_path(value: str, working_dir: str) -> str:
    """`~` expands; a relative path is relative to the project directory."""
    value = os.path.expanduser(value)
    return value if os.path.isabs(value) else os.path.join(working_dir, value)


@dataclass
class Config:
    """Holds everything the CLI/REPL and the agent loop need at runtime."""

    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    vision_model: str = DEFAULT_VISION_MODEL
    # Small, quick model for simple tasks (see forge/routing.py). Empty
    # disables automatic model selection: everything uses `model`.
    fast_model: str = ""
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    shell_timeout: int = DEFAULT_SHELL_TIMEOUT
    # Voice input (see forge/voice.py). Empty `voice_record` auto-detects a
    # recorder; `voice_transcribe` has no default and must be set to use it.
    voice_record: str = ""
    voice_transcribe: str = ""
    voice_seconds: int = DEFAULT_VOICE_SECONDS
    budget_tokens: int = DEFAULT_BUDGET_TOKENS
    budget_minutes: float = DEFAULT_BUDGET_MINUTES
    working_dir: str = "."
    usage_file: str = DEFAULT_USAGE_FILE
    # Where the REPL saves/resumes its conversation. Empty disables it;
    # `from_env` turns it on with a per-project default.
    session_file: str = ""
    prompt_price_per_1m: float = DEFAULT_PROMPT_PRICE_PER_1M
    completion_price_per_1m: float = DEFAULT_COMPLETION_PRICE_PER_1M
    # MCP servers to launch (forge.toml only; see forge/mcp.py).
    mcp_servers: dict[str, MCPServerConfig] = field(default_factory=dict)

    @classmethod
    def from_env(cls, working_dir: str | None = None) -> "Config":
        """Build a Config from env vars, then `forge.toml`, then defaults.

        `working_dir` (default: `FORGE_WORKING_DIR`, else `.`) is the project
        directory whose `forge.toml` is read. Raises `ConfigError` if that
        file is present but invalid.
        """
        if working_dir is None:
            working_dir = os.environ.get("FORGE_WORKING_DIR", ".")
        file = load_project_file(working_dir)

        def pick(env_name: str, key: str, default: Any, cast: Any = str) -> Any:
            if env_name in os.environ:
                return cast(os.environ[env_name])
            return file.get(key, default)

        def pick_path(env_name: str, key: str, default: str) -> str:
            if env_name in os.environ:
                return os.environ[env_name]
            if key in file:
                return _resolve_path(file[key], working_dir)
            return default

        return cls(
            model=pick("FORGE_MODEL", "model", DEFAULT_MODEL),
            host=pick("FORGE_HOST", "host", DEFAULT_HOST),
            vision_model=pick("FORGE_VISION_MODEL", "vision_model", DEFAULT_VISION_MODEL),
            fast_model=pick("FORGE_FAST_MODEL", "fast_model", ""),
            max_iterations=pick(
                "FORGE_MAX_ITERATIONS", "max_iterations", DEFAULT_MAX_ITERATIONS, int
            ),
            shell_timeout=pick(
                "FORGE_SHELL_TIMEOUT", "shell_timeout", DEFAULT_SHELL_TIMEOUT, int
            ),
            voice_record=pick("FORGE_VOICE_RECORD", "voice_record", ""),
            voice_transcribe=pick("FORGE_VOICE_TRANSCRIBE", "voice_transcribe", ""),
            voice_seconds=pick("FORGE_VOICE_SECONDS", "voice_seconds", DEFAULT_VOICE_SECONDS, int),
            budget_tokens=pick(
                "FORGE_BUDGET_TOKENS", "budget_tokens", DEFAULT_BUDGET_TOKENS, int
            ),
            budget_minutes=pick(
                "FORGE_BUDGET_MINUTES", "budget_minutes", DEFAULT_BUDGET_MINUTES, float
            ),
            working_dir=working_dir,
            usage_file=pick_path("FORGE_USAGE_FILE", "usage_file", DEFAULT_USAGE_FILE),
            session_file=pick_path(
                "FORGE_SESSION_FILE", "session_file", session.default_path(working_dir)
            ),
            mcp_servers=_parse_mcp_servers(
                os.path.join(working_dir, CONFIG_FILENAME), file.get("mcp_servers", {})
            ),
            prompt_price_per_1m=pick(
                "FORGE_PROMPT_PRICE_PER_1M",
                "prompt_price_per_1m",
                DEFAULT_PROMPT_PRICE_PER_1M,
                float,
            ),
            completion_price_per_1m=pick(
                "FORGE_COMPLETION_PRICE_PER_1M",
                "completion_price_per_1m",
                DEFAULT_COMPLETION_PRICE_PER_1M,
                float,
            ),
        )
