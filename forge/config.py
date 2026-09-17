"""Runtime configuration for forge."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_MODEL = "qwen2.5-coder"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_MAX_ITERATIONS = 25
DEFAULT_SHELL_TIMEOUT = 60
DEFAULT_USAGE_FILE = os.path.join(os.path.expanduser("~"), ".forge", "usage.json")

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


@dataclass
class Config:
    """Holds everything the CLI/REPL and the agent loop need at runtime."""

    model: str = DEFAULT_MODEL
    host: str = DEFAULT_HOST
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    shell_timeout: int = DEFAULT_SHELL_TIMEOUT
    working_dir: str = "."
    usage_file: str = DEFAULT_USAGE_FILE
    prompt_price_per_1m: float = DEFAULT_PROMPT_PRICE_PER_1M
    completion_price_per_1m: float = DEFAULT_COMPLETION_PRICE_PER_1M

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
            usage_file=os.environ.get("FORGE_USAGE_FILE", DEFAULT_USAGE_FILE),
            prompt_price_per_1m=float(
                os.environ.get("FORGE_PROMPT_PRICE_PER_1M", DEFAULT_PROMPT_PRICE_PER_1M)
            ),
            completion_price_per_1m=float(
                os.environ.get(
                    "FORGE_COMPLETION_PRICE_PER_1M", DEFAULT_COMPLETION_PRICE_PER_1M
                )
            ),
        )
