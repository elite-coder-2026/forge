"""Runtime configuration for forge."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_MODEL = "qwen2.5-coder"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MAX_ITERATIONS = 25
DEFAULT_SHELL_TIMEOUT = 60


@dataclass
class Config:
    """Holds everything the CLI/REPL and the agent loop need at runtime."""

    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    shell_timeout: int = DEFAULT_SHELL_TIMEOUT
    working_dir: str = "."

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config, letting environment variables override defaults."""
        return cls(
            model=os.environ.get("FORGE_MODEL", DEFAULT_MODEL),
            host=os.environ.get("FORGE_HOST", DEFAULT_HOST),
            max_iterations=int(
                os.environ.get("FORGE_MAX_ITERATIONS", DEFAULT_MAX_ITERATIONS)
            ),
            shell_timeout=int(
                os.environ.get("FORGE_SHELL_TIMEOUT", DEFAULT_SHELL_TIMEOUT)
            ),
            working_dir=os.environ.get("FORGE_WORKING_DIR", "."),
        )
