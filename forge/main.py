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

from . import gitutil, llm, session
from .config import Config

HELP_TEXT = """\
Commands:
  /help            Show this help.
  /clear           Clear the conversation history and saved session (starts fresh).
  /model <name>    Switch to a different model for subsequent tasks.
  /pull <name>     Pull a model via `ollama pull`.
  /usage           Show this session's + all-time token usage and estimated $ saved.
  /plan            Enter plan mode: read-only tools only, no edits or shell commands.
  /build           Exit plan mode: full tool access again.
  /exit, /quit     Exit the REPL.
Anything else is sent to the model as a task."""


class REPLExit(Exception):
    """Raised by a slash command to signal the REPL loop should stop."""


@dataclass
class REPLState:
    config: Config
    client: Any
    history: list[dict[str, Any]] = field(default_factory=llm.new_history)
    plan_mode: bool = False


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge", description="A local agentic coding CLI.")
    parser.add_argument("--model", default=None, help="Override the model to use for this run.")
    parser.add_argument("--host", default=None, help="Override the Ollama host to use for this run.")
    parser.add_argument("-i", "--interactive", action="store_true", help="Start an interactive REPL.")
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Plan mode: read-only tools only, no edits or shell commands.",
    )
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
        session.clear(state.config.session_file)
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
        return _format_usage(state.config)

    if name == "/plan":
        state.plan_mode = True
        return "Entered plan mode: read-only tools only. /build to exit."

    if name == "/build":
        state.plan_mode = False
        return "Exited plan mode: full tool access restored."

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


def _estimate_savings(usage: dict[str, int], config: Config) -> float:
    return (
        usage["prompt_tokens"] / 1_000_000 * config.prompt_price_per_1m
        + usage["completion_tokens"] / 1_000_000 * config.completion_price_per_1m
    )


def _format_usage(config: Config) -> str:
    session = llm.get_usage()
    all_time = llm.get_persisted_usage(config.usage_file)
    saved = _estimate_savings(all_time, config)

    return (
        f"This session — calls: {session['calls']}  "
        f"prompt: {session['prompt_tokens']}  completion: {session['completion_tokens']}\n"
        f"All-time (running total)  — calls: {all_time['calls']}  "
        f"prompt: {all_time['prompt_tokens']}  completion: {all_time['completion_tokens']}\n"
        f"Estimated money saved vs. paying for hosted {config.model}-class "
        f"inference (${config.prompt_price_per_1m:.2f}/1M in, "
        f"${config.completion_price_per_1m:.2f}/1M out): ${saved:.4f}"
    )


# ---------------------------------------------------------------------------
# REPL / one-shot execution
# ---------------------------------------------------------------------------


def _error_message(error: llm.LLMError, config: Config) -> str:
    if isinstance(error, llm.OllamaUnreachableError):
        return (
            f"Ollama isn't reachable at {config.host}. "
            f"Is it running? (start it with `ollama serve`)"
        )
    return str(error)


def _git_report(before: gitutil.Snapshot | None, task: str, interactive: bool) -> None:
    """After a task: summarize the files it changed and offer to commit them.

    Interactive (REPL) sessions are asked before anything is committed;
    one-shot runs only print a suggested message. Never pushes.
    """
    if before is None:
        return
    after = gitutil.snapshot(before.root)
    files = gitutil.changed_files(before, after) if after else []
    if not files:
        return

    print()
    print(gitutil.diff_summary(after, files))
    message = gitutil.suggest_message(task)

    if not interactive:
        print(f'Suggested commit message: "{message}"')
        return

    try:
        answer = input(f'Commit these files with message "{message}"? [y/N] ')
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer.strip().lower() in ("y", "yes"):
        ok, output = gitutil.commit(after.root, files, message)
        print(output if output else ("Committed." if ok else "Commit failed."))


class _LivePrinter:
    """`on_token` callback that prints streamed text as it arrives.

    `finish()` ends the streamed line, or prints `fallback` if nothing was
    streamed (so the final answer is never printed twice).
    """

    def __init__(self) -> None:
        self.streamed = False

    def __call__(self, token: str) -> None:
        self.streamed = True
        print(token, end="", flush=True)

    def finish(self, fallback: str = "") -> None:
        if self.streamed:
            print()
        elif fallback:
            print(fallback)


def run_repl(config: Config, client: Any, plan_mode: bool = False) -> None:
    state = REPLState(config=config, client=client, plan_mode=plan_mode)
    print(f"forge REPL — model: {config.model}. Type /help for commands, /exit to quit.")
    if state.plan_mode:
        print("Starting in plan mode (read-only). /build to exit.")

    resumed = session.load(config.session_file)
    if resumed:
        state.history = resumed
        print(f"Resumed previous session ({len(resumed)} messages). /clear starts fresh.")

    while True:
        try:
            line = input(_repl_prompt(state))
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

        printer = _LivePrinter()
        before = None if state.plan_mode else gitutil.snapshot(state.config.working_dir)
        try:
            result = llm.run_task(
                line,
                state.history,
                state.client,
                state.config.model,
                base_dir=state.config.working_dir,
                shell_timeout=state.config.shell_timeout,
                max_iterations=state.config.max_iterations,
                usage_file=state.config.usage_file,
                read_only=state.plan_mode,
                on_token=printer,
            )
        except llm.LLMError as e:
            printer.finish()
            print(f"Error: {_error_message(e, state.config)}")
            continue

        state.history = result.history
        session.save(state.config.session_file, state.history)
        printer.finish(fallback=result.content)
        _git_report(before, line, interactive=True)


def _repl_prompt(state: REPLState) -> str:
    return "[plan] > " if state.plan_mode else "> "


def run_once(task: str, config: Config, client: Any, plan_mode: bool = False) -> int:
    printer = _LivePrinter()
    before = None if plan_mode else gitutil.snapshot(config.working_dir)
    try:
        result = llm.run_task(
            task,
            llm.new_history(),
            client,
            config.model,
            base_dir=config.working_dir,
            shell_timeout=config.shell_timeout,
            max_iterations=config.max_iterations,
            usage_file=config.usage_file,
            read_only=plan_mode,
            on_token=printer,
        )
    except llm.LLMError as e:
        printer.finish()
        print(f"Error: {_error_message(e, config)}", file=sys.stderr)
        return 1

    printer.finish(fallback=result.content)
    _git_report(before, task, interactive=False)
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
        run_repl(config, client, plan_mode=args.plan)
        return 0

    return run_once(task, config, client, plan_mode=args.plan)


if __name__ == "__main__":
    sys.exit(main())
