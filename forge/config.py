"""Runtime configuration for forge.

Precedence, highest first: CLI flags, environment variables, a
`forge.toml` in the project directory, then the built-in defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
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


# Keys allowed in forge.toml, with the type each must be. (The working
# directory isn't one: it's what locates the file in the first place.)
_FILE_KEYS: dict[str, tuple[type, ...]] = {
    "model": (str,),
    "host": (str,),
    "vision_model": (str,),
    "fast_model": (str,),
    "max_iterations": (int,),
    "shell_timeout": (int,),
    "usage_file": (str,),
    "session_file": (str,),
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
    return data


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
    working_dir: str = "."
    usage_file: str = DEFAULT_USAGE_FILE
    # Where the REPL saves/resumes its conversation. Empty disables it;
    # `from_env` turns it on with a per-project default.
    session_file: str = ""
    prompt_price_per_1m: float = DEFAULT_PROMPT_PRICE_PER_1M
    completion_price_per_1m: float = DEFAULT_COMPLETION_PRICE_PER_1M

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config from env vars, then `forge.toml`, then defaults.

        Raises `ConfigError` if `forge.toml` is present but invalid.
        """
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
            working_dir=working_dir,
            usage_file=pick_path("FORGE_USAGE_FILE", "usage_file", DEFAULT_USAGE_FILE),
            session_file=pick_path(
                "FORGE_SESSION_FILE", "session_file", session.default_path(working_dir)
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
