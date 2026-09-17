"""CLI entry point and REPL for forge.

Note: `forge/__init__.py` deliberately does NOT do `from .main import
main` — that used to shadow the `forge.main` *submodule* with the `main`
*function* of the same name, so `import forge.main` handed back a
function instead of a module and blew up with a confusing
`AttributeError` anywhere that expected the module. The installed CLI
entry point (see `pyproject.toml`: `forge = "forge.main:main"`) never
needed that import, so it's just gone.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any

import ollama

from . import llm
from .config import Config

HELP_TEXT = """\
Commands:
  /help            Show this help.
  /clear           Clear the conversation history (starts a new session).
  /model <name>    Switch to a different model for subsequent tasks.
  /pull <name>     Pull a model via `ollama pull`.
  /usage           Show cumulative token usage for this process.
  /exit, /quit     Exit the REPL.
Anything else is sent to the model as a task."""


class REPLExit(Exception):
    """Raised by a slash command to signal the REPL loop should stop."""


@dataclass
class REPLState:
    config: Config
    client: Any
    history: list[dict[str, Any]] = field(default_factory=llm.new_history)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description="A local agentic coding CLI.")
    parser.add_argument("--model", default=None, help="Override the model to use for this run.")
    parser.add_argument("--host", default=None, help="Override the Ollama host to use for this run.")
    parser.add_argument("-i", "--interactive", action="store_true", help="Start an interactive REPL.")
    return parser


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, str | None]:
    """Parse argv, tolerating a task that itself looks like a flag.

    Uses `parse_known_args` instead of `parse_args`/a positional
    `nargs="?"` argument: with a plain positional, a task like `-fix`
    is indistinguishable from an unrecognized option and argparse
    aborts the whole process. `parse_known_args` never errors on
    unrecognized tokens — it just returns them — so every token that
    isn't one of forge's own flags (recognized or not, dash-prefixed or
    not) is treated as part of the task, verbatim, and joined back
    together in order.
    """
    parser = build_arg_parser()
    args, extras = parser.parse_known_args(argv)
    task = " ".join(extras).strip() if extras else None
    return args, task


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------


def handle_slash_command(line: str, state: REPLState) -> str:
    """Handle one REPL line that starts with '/'. Returns the text to
    print. Raises `REPLExit` for /exit and /quit.

    Every command here validates its own arguments and returns a usage
    message on bad input instead of raising (an empty or malformed
    `/model` argument must not crash the REPL).
    """
    parts = line.strip().split(maxsplit=1)
    name = parts[0] if parts else ""
    arg_str = parts[1].strip() if len(parts) > 1 else ""

    if name in ("/exit", "/quit"):
        raise REPLExit()

    if name == "/clear":
        state.history = llm.new_history()
        return "History cleared."

    if name == "/model":
        if not arg_str:
            return "Usage: /model <name>"
        state.config.model = arg_str
        return f"Model set to {arg_str!r}"

    if name == "/pull":
        if not arg_str:
            return "Usage: /pull <name>"
        return _pull_model(arg_str)

    if name == "/usage":
        usage = llm.get_usage()
        return (
            f"Calls: {usage['calls']}  "
            f"Prompt tokens: {usage['prompt_tokens']}  "
            f"Completion tokens: {usage['completion_tokens']}  "
            f"Total: {usage['prompt_tokens'] + usage['completion_tokens']}"
        )

    if name == "/help":
        return HELP_TEXT

    return f"Unknown command: {name!r} (try /help)"


def _pull_model(model_name: str) -> str:
    """Run `ollama pull <model_name>`, streaming its progress straight to
    the terminal (stdout/stderr are inherited, not captured, since it's a
    long-running download with a live progress bar).
    """
    try:
        result = subprocess.run(["ollama", "pull", model_name])
    except FileNotFoundError:
        return "Error: 'ollama' command not found on PATH"
    except OSError as e:
        return f"Error: failed to run 'ollama pull': {e}"

    if result.returncode != 0:
        return f"Error: 'ollama pull {model_name}' exited with code {result.returncode}"
    return f"Pulled model {model_name!r}"


# ---------------------------------------------------------------------------
# REPL / one-shot execution
# ---------------------------------------------------------------------------


def run_repl(config: Config, client: Any) -> None:
    state = REPLState(config=config, client=client)
    print(f"forge REPL — model: {config.model}. Type /help for commands, /exit to quit.")

    while True:
        try:
            line = input("> ")
        except (EOFError, KeyboardInterrupt):
            print()
            return

        line = line.strip()
        if not line:
            continue

        if line.startswith("/"):
            try:
                output = handle_slash_command(line, state)
            except REPLExit:
                return
            print(output)
            continue

        try:
            result = llm.run_task(
                line,
                state.history,
                state.client,
                state.config.model,
                base_dir=state.config.working_dir,
                shell_timeout=state.config.shell_timeout,
                max_iterations=state.config.max_iterations,
            )
        except llm.LLMError as e:
            print(f"Error: {e}")
            continue

        state.history = result.history
        print(result.content)


def run_once(task: str, config: Config, client: Any) -> int:
    try:
        result = llm.run_task(
            task,
            llm.new_history(),
            client,
            config.model,
            base_dir=config.working_dir,
            shell_timeout=config.shell_timeout,
            max_iterations=config.max_iterations,
        )
    except llm.LLMError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    print(result.content)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args, task = parse_args(argv)

    config = Config.from_env()
    if args.model:
        config.model = args.model
    if args.host:
        config.host = args.host

    client = ollama.Client(host=config.host)

    if args.interactive or task is None:
        run_repl(config, client)
        return 0

    return run_once(task, config, client)


if __name__ == "__main__":
    sys.exit(main())
