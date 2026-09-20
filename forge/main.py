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
import atexit
import os
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Any

import ollama

from . import budget, chatui, gitutil, llm, mcp, modes, plugins, routing, session, tools, ui, undo, vision, voice, webui
from . import __version__
from .config import Config, ConfigError

HELP_TEXT = """\
Commands:
  /help            Show this help.
  /clear           Clear the conversation history and saved session (starts fresh).
  /model           List the models available on the Ollama server.
  /pull <name>     Pull a model via `ollama pull`.
  /model <name>    Switch to a different model for subsequent tasks.
  /usage           Show this session's + all-time token usage and estimated $ saved.
  /voice           Dictate the next task (needs voice_transcribe; see README).
  /plugins         List loaded plugin tools (custom tools from .forge/plugins).
  /mcp             List connected MCP servers and their tools.
  /budget          Show session token/compute budget (/budget tokens N, /budget minutes N; 0 or off disables).
  /auto [on|off]   Show or toggle automatic model selection (needs a fast model).
  /fast <name>     Set the fast model used for simple tasks (/fast off to clear).
  /session [list|new <dir> [name]|switch <name>|close <name>]  Work in several project directories at once.
  /undo [N|list|force]  Revert the last N file changes forge made (default 1). Not for shell-made changes.
  /image <path>    Attach image(s) to the next task (screenshot -> code). /image alone lists; /image clear drops them.
  /plan            Enter plan mode: read-only tools only, no edits or shell commands.
  /build           Exit plan mode: full tool access again.
  /mode [name]     Show or set the permission mode: default (ask before edits and commands),
                   auto (edits go through, commands still ask), plan (read-only), dangerous (never ask).
  /exit, /quit    Exit the REPL.
Anything else is sent to the model as a task."""


class REPLExit(Exception):
    """Raised by a slash command to signal the REPL loop should stop."""


@dataclass
class Workspace:
    """The named sessions open in one REPL, and which one is active."""

    sessions: dict[str, "REPLState"] = field(default_factory=dict)
    current: str = ""

    @property
    def state(self) -> "REPLState":
        return self.sessions[self.current]


@dataclass
class REPLState:
    config: Config
    client: Any
    history: list[dict[str, Any]] = field(default_factory=llm.new_history)
    plan_mode: bool = False
    pending_images: list[str] = field(default_factory=list)
    auto_model: bool = True
    edit_mode: str = "default"  # default | auto | dangerous (plan is `plan_mode`)
    always_allowed: set[str] = field(default_factory=set)  # "edits"/"shell" answered "always"
    budget: budget.BudgetTracker = field(default_factory=budget.BudgetTracker)
    last_model: str | None = None  # model used for the previous task
    name: str = "main"
    workspace: Workspace | None = None


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
    parser.add_argument(
        "--voice",
    parser.add_argument(
        "--mode",
        choices=modes.MODES,
        default=None,
        help="Permission mode: default (ask before edits and commands), auto (edits go through), "
        "plan (read-only), dangerous (never ask).",
    )
    parser.add_argument(
        "--dangerous-edits",
        action="store_true",
        help="Same as --mode dangerous: edits and shell commands run without asking.",
    )
        action="store_true",
        help="Dictate the task instead of typing it (needs voice_transcribe; see README).",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Serve a read-only usage/history dashboard on localhost while forge runs (best with -i).",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=webui.DEFAULT_PORT,
        metavar="PORT",
        help=f"Port for --web (default {webui.DEFAULT_PORT}; 0 picks a free one).",
    )
    parser.add_argument(
        "--chat",
        action="store_true",
        help="Serve a chat page on localhost so you can send tasks from a browser (best with -i).",
    )
    parser.add_argument(
        "--chat-port",
        type=int,
        default=chatui.DEFAULT_PORT,
        metavar="PORT",
        help=f"Port for --chat (default {chatui.DEFAULT_PORT}; 0 picks a free one).",
    )
    parser.add_argument(
        "--trust-plugins",
        action="store_true",
        help="Approve this project's .forge/plugins without asking (remembered until they change).",
    )
    parser.add_argument(
        "--trust-mcp",
        action="store_true",
        help="Approve the MCP servers in this project's forge.toml without asking (remembered until they change).",
    )
    parser.add_argument(
        "--image",
        action="append",
        default=[],
        metavar="PATH",
        help="Attach a screenshot/mockup (repeatable). A vision model describes it for the coding model.",
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
            return _list_models(state)
        state.config.model = arg_str
        message = f"Model set to {arg_str!r}"
        if state.config.fast_model and state.auto_model:
            # An explicit choice beats automatic routing.
            state.auto_model = False
            message += " (auto model selection off; /auto on to re-enable)"
        return message

    if name == "/auto":
        return _handle_auto_command(arg_str, state)

    if name == "/budget":
        return _handle_budget_command(arg_str, state)

    if name in ("/session", "/sessions"):
        return _handle_session_command(arg_str, state)

    if name == "/plugins":
        registry = plugins.get_registry()
        if registry is None:
            return "No plugins loaded (add .py files to ~/.forge/plugins or <project>/.forge/plugins)."
        return registry.describe()

    if name == "/mcp":
        manager = mcp.get_manager()
        if manager is None:
            return "No MCP servers connected (declare them as [mcp_servers.<name>] tables in forge.toml)."
        return manager.describe()

    if name == "/fast":
        if not arg_str:
            return "Usage: /fast <name>  (or /fast off)"
        if arg_str == "off":
            state.config.fast_model = ""
            return "Fast model cleared; every task uses the main model."
        state.config.fast_model = arg_str
        state.auto_model = True
        return f"Fast model set to {arg_str!r}; simple tasks will use it (main: {state.config.model!r})."

    if name == "/pull":
        if not arg_str:
            return "Usage: /pull <name>"
        return _pull_model(arg_str)

    if name == "/usage":
        return _format_usage(state.config)

    if name == "/image":
        return _handle_image_command(arg_str, state)

    if name == "/undo":
        return _handle_undo_command(arg_str, state)

    if name == "/plan":
        state.plan_mode = True
        return "Entered plan mode: read-only tools only. /build to exit."

    if name == "/build":
        state.plan_mode = False
        return "Exited plan mode: full tool access restored."

    if name == "/help":
        return HELP_TEXT
    if name == "/mode":
        return _handle_mode_command(arg_str, state)


    return f"Unknown command: {name!r} (try /help)"


def _field(item: Any, key: str) -> Any:
    """A field of an Ollama response item, whether it's a dict or an object."""
    return item.get(key) if isinstance(item, dict) else getattr(item, key, None)


def _installed_models(client: Any) -> list[tuple[str, Any]]:
    """(name, size) for each model on the Ollama server, sorted by name."""
    found = _field(client.list(), "models") or []
    return sorted((_field(m, "model") or _field(m, "name") or "?", _field(m, "size")) for m in found)


def _list_models(state: REPLState) -> str:
    """The models the Ollama server has, with the current one marked."""
    try:
        rows = _installed_models(state.client)
    except Exception as e:  # noqa: BLE001 - the server may be down; say so, don't crash
        return f"Could not list models: {type(e).__name__}: {e}\nUsage: /model <name>"
    if not rows:
        return "No models found. /pull <name> to download one."
    lines = ["Available models (* = current):"]
    for name, size in rows:
        mark = "*" if name == state.config.model else " "
        lines.append(f"  {mark} {name}" + (f"  ({size / 1e9:.1f} GB)" if size else ""))
    lines.append("/model <name> to switch, /pull <name> to download another.")
    return "\n".join(lines)


        f"{state.config.vision_model} and sent with your next task."
    )
def _handle_mode_command(arg_str: str, state: REPLState) -> str:
    wanted = arg_str.strip().lower()
    if not wanted:
        return f"Mode: {modes.label(state.plan_mode, state.edit_mode)} (choose from {', '.join(modes.MODES)})."
    if wanted not in modes.MODES:
        return f"Unknown mode {wanted!r}. Choose from {', '.join(modes.MODES)}."
    if wanted == "plan":
        state.plan_mode = True
        return "Mode: plan (read-only tools only). /mode default, /mode auto or /build to leave."
    state.plan_mode = False
    state.edit_mode = wanted
    if wanted == "dangerous":
        return "Mode: dangerous. Edits and shell commands run WITHOUT asking."
    return f"Mode: {modes.label(False, wanted)}."


def _handle_image_command(arg_str: str, state: REPLState) -> str:
    if not arg_str:
        if not state.pending_images:
            return "No images attached. Usage: /image <path> [<path> ...]"
        return "Attached for the next task:\n" + "\n".join(f"  {p}" for p in state.pending_images)
    if arg_str == "clear":
        state.pending_images = []
        return "Attached images cleared."

    try:
        paths = shlex.split(arg_str)
    except ValueError as e:
        return f"Error: could not parse paths: {e}"
    try:
        resolved = [vision.resolve_image(p, state.config.working_dir) for p in paths]
    except vision.VisionError as e:
        return f"Error: {e}"

    state.pending_images.extend(p for p in resolved if p not in state.pending_images)
    return (
        f"{len(state.pending_images)} image(s) attached; they'll be described by "


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _load_plugins(config: Config, trust_flag: bool) -> None:
    """Load ~/.forge/plugins (yours) and, once approved, <project>/.forge/plugins.

    Project plugins are code that runs inside forge, so a repo can't
    supply them unasked: approval is prompted once (or `--trust-plugins`),
    remembered for exactly those file contents, and asked again if they
    change. Status goes to stderr so stdout stays clean.
    """
    project_dir = os.path.join(config.working_dir, plugins.PROJECT_PLUGIN_SUBDIR)
    project_files = plugins.plugin_files(project_dir)
    user_files = plugins.plugin_files(plugins.USER_PLUGIN_DIR)
    if not project_files and not user_files:
        return

    load_project = False
    if project_files:
        if plugins.is_trusted(config.working_dir, project_dir):
            load_project = True
        elif trust_flag:
            plugins.trust(config.working_dir, project_dir)
            load_project = True
        elif _is_interactive():
            ui.emit("This project has plugins that would run inside forge:", err=True)
            for path in project_files:
                ui.emit(f"  {os.path.relpath(path, config.working_dir)}", err=True)
            try:
                answer = ui.ask("Load them? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() in ("y", "yes"):
                plugins.trust(config.working_dir, project_dir)
                load_project = True
            else:
                ui.emit("Project plugins not loaded.", err=True)
        else:
            ui.emit(
                "Project plugins in .forge/plugins were not loaded: they haven't been approved. "
                "Run forge interactively once to approve them, or pass --trust-plugins.",
                err=True,
            )

    registry = plugins.PluginRegistry()
    reserved = {s["function"]["name"] for s in tools.TOOL_SCHEMAS}
    registry.load(plugins.USER_PLUGIN_DIR, reserved)
    if load_project:
        registry.load(project_dir, reserved)
    plugins.set_registry(registry)

    if registry.tools:
        ui.emit(f"Plugins: loaded {', '.join(registry.tools)}", err=True)
    for path, error in registry.errors.items():
        ui.emit(f"Plugins: {os.path.basename(path)} failed to load: {error}", err=True)


def _start_mcp(config: Config, trust_flag: bool) -> None:
    """Launch the MCP servers declared in forge.toml, if the user approves.

    A repo's forge.toml names programs to run, so it can't start them on
    its own: approval is asked once (interactively) or given with
    `--trust-mcp`, remembered for exactly these definitions, and asked
    again if they change. Status goes to stderr so stdout stays clean.
    """
    if not config.mcp_servers:
        return

    if not mcp.is_trusted(config.working_dir, config.mcp_servers):
        if trust_flag:
            mcp.trust(config.working_dir, config.mcp_servers)
        elif _is_interactive():
            ui.emit("This project's forge.toml wants to run these MCP servers:", err=True)
            for name, server in config.mcp_servers.items():
                ui.emit(f"  {name}: {' '.join([server.command, *server.args])}", err=True)
            try:
                answer = ui.ask("Allow them? [y/N] ")
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                ui.emit("MCP servers not started.", err=True)
                return
            mcp.trust(config.working_dir, config.mcp_servers)
        else:
            ui.emit(
                "MCP servers in forge.toml were not started: they haven't been approved. "
                "Run forge interactively once to approve them, or pass --trust-mcp.",
                err=True,
            )
            return

    manager = mcp.MCPManager()
    manager.start(config.mcp_servers, config.working_dir)
    mcp.set_manager(manager)

    connected = [
        f"{name} ({sum(1 for t in manager.tools.values() if t.server == name)} tools)"
        for name in manager.clients
    ]
    if connected:
        ui.emit(f"MCP: connected {', '.join(connected)}", err=True)
    for name, error in manager.errors.items():
        ui.emit(f"MCP: server {name!r} failed to start: {error}", err=True)


def _handle_session_command(arg_str: str, state: REPLState) -> str:
    usage = "Usage: /session [list | new <dir> [name] | switch <name> | close <name>]"
    workspace = state.workspace
    if workspace is None:
        return "Sessions aren't available here."
    try:
        parts = shlex.split(arg_str)
    except ValueError as e:
        return f"Error: could not parse arguments: {e}"
    sub = parts[0] if parts else "list"

    if sub == "list" and len(parts) <= 1:
        lines = []
        for name, s in workspace.sessions.items():
            marker = "*" if name == workspace.current else " "
            extra = ", plan mode" if s.plan_mode else ""
            lines.append(
                f"{marker} {name}: {os.path.realpath(s.config.working_dir)} "
                f"({len(s.history)} messages{extra})"
            )
        return "\n".join(lines)

    if sub == "new" and len(parts) in (2, 3):
        directory = os.path.realpath(
            os.path.join(state.config.working_dir, os.path.expanduser(parts[1]))
        )
        if not os.path.isdir(directory):
            return f"Error: not a directory: {directory}"
        for other in workspace.sessions.values():
            if os.path.realpath(other.config.working_dir) == directory:
                return f"Error: {directory} is already open as session {other.name!r}"

        name = parts[2] if len(parts) == 3 else (os.path.basename(directory) or "session")
        if len(parts) == 3 and name in workspace.sessions:
            return f"Error: a session named {name!r} already exists"
        base, n = name, 1
        while name in workspace.sessions:  # only reachable for the derived name
            n += 1
            name = f"{base}-{n}"

        try:
            config = Config.from_env(directory)  # that project's own forge.toml
        except ConfigError as e:
            return f"Error: {e}"
        config.host = state.config.host  # one Ollama server for every session

        opened = REPLState(
            config=config,
            client=state.client,
            budget=state.budget,  # usage totals are process-wide, so is the budget
            name=name,
            workspace=workspace,
        )
        resumed = session.load(config.session_file)
        if resumed:
            opened.history = resumed
        workspace.sessions[name] = opened
        workspace.current = name
        note = f" (resumed {len(resumed)} messages)" if resumed else ""
        return f"Opened session {name!r} in {directory}{note}. Now working there."

    if sub == "switch" and len(parts) == 2:
        if parts[1] not in workspace.sessions:
            return f"Error: no session named {parts[1]!r} (see /session list)"
        workspace.current = parts[1]
        return f"Switched to session {parts[1]!r}."

    if sub == "close" and len(parts) == 2:
        if parts[1] not in workspace.sessions:
            return f"Error: no session named {parts[1]!r} (see /session list)"
        if len(workspace.sessions) == 1:
            return "Error: can't close the only session (use /exit to quit)."
        del workspace.sessions[parts[1]]
        message = f"Closed session {parts[1]!r} (its saved history is kept)."
        if workspace.current == parts[1]:
            workspace.current = next(iter(workspace.sessions))
            message += f" Now in {workspace.current!r}."
        return message

    return usage


# What the dashboard reads. `run_repl` publishes its workspace here; the
# dashboard thread only ever reads it.
_web_workspace: Workspace | None = None
_WEB_MAX_MESSAGES = 200
_WEB_MAX_CONTENT = 4000


def _web_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content") or ""
    if not isinstance(content, str):
        content = str(content)
    if len(content) > _WEB_MAX_CONTENT:
        content = content[:_WEB_MAX_CONTENT] + f"\n... [{len(content) - _WEB_MAX_CONTENT} more characters]"

    calls = []
    for call in message.get("tool_calls") or []:
        try:
            calls.append(str(call["function"]["name"]))
        except (KeyError, TypeError):
            calls.append("?")
    entry: dict[str, Any] = {"role": str(message.get("role", "?")), "content": content}
    if calls:
        entry["tool_calls"] = calls
    if message.get("name"):
        entry["name"] = str(message["name"])
    return entry


def _web_snapshot(config: Config) -> dict[str, Any]:
    """A JSON-safe, read-only picture of usage and every open session."""
    workspace = _web_workspace
    sessions = []
    if workspace is not None:
        for name, s in list(workspace.sessions.items()):
            history = list(s.history)  # copy: the REPL replaces, but never mutates, it mid-task
            sessions.append(
                {
                    "name": name,
                    "current": name == workspace.current,
                    "directory": os.path.realpath(s.config.working_dir),
                    "model": s.config.model,
                    "plan_mode": s.plan_mode,
                    "message_count": len(history),
                    "messages": [_web_message(m) for m in history[-_WEB_MAX_MESSAGES:]],
                    "changes": undo.history(s.config.working_dir),
                }
            )

    all_time = llm.get_persisted_usage(config.usage_file)
    return {
        "sessions": sessions,
        "usage": {
            "session": llm.get_usage(),
            "all_time": all_time,
            "compute_seconds": llm.get_compute_seconds(),
            "estimated_saved_usd": _estimate_savings(all_time, config),
        },
        "budget": {
            "tokens_limit": config.budget_tokens,
            "minutes_limit": config.budget_minutes,
        },
    }


def _start_web(config: Config, port: int) -> webui.Dashboard | None:
    try:
        dashboard = webui.Dashboard(lambda: _web_snapshot(config), port)
    except OSError as e:
        ui.emit(f"Dashboard not started: could not listen on port {port}: {e}", err=True)
        return None
    atexit.register(dashboard.stop)
    ui.emit(f"Dashboard: {dashboard.url} (read-only, localhost only)", err=True)
    return dashboard


_chat_lock = threading.Lock()  # the browser runs one task at a time


def _chat_history() -> list[dict[str, str]]:
    """The active session's user/assistant turns, for the chat page to show on load."""
    workspace = _web_workspace
    if workspace is None:
        return []
    return [
        {"role": m["role"], "content": m["content"]}
        for m in list(workspace.state.history)
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"]
    ]


def _chat_reply(message: str) -> str:
    """Run one browser-sent message as a task on the active REPL session.

    Honours the session's plan/build mode and model routing. Errors are
    raised, so the chat page shows them.
    """
    workspace = _web_workspace
    if workspace is None:
        raise RuntimeError("forge isn't ready yet")
    with _chat_lock:
        state = workspace.state
        choice = routing.choose_model(
            message,
            state.config.model,
            state.config.fast_model if state.auto_model else "",
            images=False,
            plan_mode=state.plan_mode,
            last=state.last_model,
        )
        try:
            # The artifact prompt is chat-only: sent as a system message for
            # this task, then dropped so it never lands in the shared history.
            result = llm.run_task(
                message,
                [{"role": "system", "content": chatui.ARTIFACT_PROMPT}, *state.history],
                state.client,
                choice.model,
                base_dir=state.config.working_dir,
                shell_timeout=state.config.shell_timeout,
                max_iterations=state.config.max_iterations,
                usage_file=state.config.usage_file,
                read_only=state.plan_mode,
                think=state.config.think_setting,
            )
        except llm.LLMError as e:
            raise RuntimeError(_error_message(e, state.config)) from e
        state.history = result.history[1:]
        state.last_model = choice.model
        session.save(state.config.session_file, state.history)
        return result.content


def _start_chat(port: int) -> chatui.ChatServer | None:
    try:
        server = chatui.ChatServer(_chat_reply, _chat_history, port)
    except OSError as e:
        ui.emit(f"Chat page not started: could not listen on port {port}: {e}", err=True)
        return None
    atexit.register(server.stop)
    ui.emit(f"Chat: {server.url} (localhost only; tasks run in the active session)", err=True)
    return server


def _voice_task(config: Config, confirm: bool = True) -> str | None:
    """Record, transcribe and (by default) confirm a dictated task.

    Returns the task text, or None if it failed, was cancelled, or the user
    declined it. The transcript is shown first because speech-to-text
    mishears, and a wrong task can change files.
    """
    limit = max(1, config.voice_seconds)
    heard: list[str] = []
    try:
        voice.VoiceInput(
            config.voice_record,
            config.voice_transcribe,
            limit,
            heard.append,
            on_status=lambda message: ui.emit(message, flush=True),
        ).start()
        text = heard[0]
    except voice.VoiceError as e:
        ui.emit(f"Error: {e}", err=True)
        return None
    except KeyboardInterrupt:
        ui.emit("\nCancelled.", err=True)
        return None

    ui.emit(f'Heard: "{text}"')
    if not confirm:
        return text
    try:
        answer = ui.ask("Send this? [Y/n] ")
    except (EOFError, KeyboardInterrupt):
        ui.emit()
        return None
    return text if answer.strip().lower() in ("", "y", "yes") else None


def _session_totals() -> tuple[int, float]:
    usage = llm.get_usage()
    return usage["prompt_tokens"] + usage["completion_tokens"], llm.get_compute_seconds()


def _handle_budget_command(arg_str: str, state: REPLState) -> str:
    usage_hint = "Usage: /budget [tokens|minutes <N|off>]"
    tokens, seconds = _session_totals()
    parts = arg_str.split()

    if parts:
        if len(parts) != 2 or parts[0] not in ("tokens", "minutes"):
            return usage_hint
        raw = parts[1].lower()
        try:
            value = 0.0 if raw == "off" else float(raw)
        except ValueError:
            return usage_hint
        if value < 0:
            return usage_hint
        if parts[0] == "tokens":
            state.budget.set_limits(tokens=int(value), used_tokens=tokens, used_seconds=seconds)
        else:
            state.budget.set_limits(minutes=value, used_tokens=tokens, used_seconds=seconds)

    return state.budget.status(tokens, seconds)


def _budget_hook(tracker: budget.BudgetTracker, printer: "_LivePrinter") -> Any:
    """An `on_step` callback: after each model call, warn if a limit is crossed."""

    def check() -> None:
        tokens, seconds = _session_totals()
        for notice in tracker.check(tokens, seconds):
            printer.notice(notice)

    return check


def _handle_auto_command(arg_str: str, state: REPLState) -> str:
    config = state.config
    if arg_str in ("on", "off"):
        if arg_str == "on" and not config.fast_model:
            return "No fast model set. Use /fast <name>, or set fast_model in forge.toml / FORGE_FAST_MODEL."
        state.auto_model = arg_str == "on"
    elif arg_str:
        return "Usage: /auto [on|off]"

    if not config.fast_model:
        return "Auto model selection: not configured (set a fast model with /fast <name>)."
    if not state.auto_model:
        return f"Auto model selection: off (using {config.model!r} for everything)."
    return f"Auto model selection: on (quick tasks: {config.fast_model!r}, larger tasks: {config.model!r})."


def _announce_model(choice: routing.Choice, err: bool = False) -> None:
    """Tell the user which model was picked, but only when routing is active."""
    if choice.reason:
        ui.emit(f"[auto] {choice.model}: {choice.reason}", err=err, flush=True)


def _handle_undo_command(arg_str: str, state: REPLState) -> str:
    tokens = arg_str.split()
    base_dir = state.config.working_dir

    if tokens == ["list"]:
        lines = undo.history(base_dir)
        return "Recent changes (newest first):\n" + "\n".join(f"  {l}" for l in lines) if lines else "Nothing to undo."

    force = "force" in tokens
    rest = [t for t in tokens if t != "force"]
    if len(rest) > 1:
        return "Usage: /undo [N|list|force]"
    count = 1
    if rest:
        if not rest[0].isdigit() or int(rest[0]) < 1:
            return "Usage: /undo [N|list|force]  (N is a positive number)"
        count = int(rest[0])

    return "\n".join(undo.undo(base_dir, count, force=force))


def _with_images(task: str, images: list[str], config: Config, client: Any) -> str:
    """`task` plus a vision-model description of `images`, if there are any."""
    if not images:
        return task
    ui.emit(f"Analyzing {len(images)} image(s) with {config.vision_model}...", flush=True)
    return vision.enrich_task(task, images, client, config.vision_model, config.usage_file)


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

    ui.emit()
    ui.emit(gitutil.diff_summary(after, files))
    message = gitutil.suggest_message(task)

    if not interactive:
        ui.emit(f'Suggested commit message: "{message}"')
        return

    try:
        answer = ui.ask(f'Commit these files with message "{message}"? [y/N] ')
    except (EOFError, KeyboardInterrupt):
        ui.emit()
        return
    if answer.strip().lower() in ("y", "yes"):
        ok, output = gitutil.commit(after.root, files, message)
        ui.emit(output if output else ("Committed." if ok else "Commit failed."))


class _LivePrinter:
    """`on_token` callback that prints streamed text as it arrives.

    `finish()` ends the streamed line, or prints `fallback` if nothing was
    streamed (so the final answer is never printed twice).
    """

    def __init__(self) -> None:
        self.streamed = False
        self._stream = ui.AssistantStream()

    def __call__(self, token: str) -> None:
        self.streamed = True
        self._stream.write(token)

    def notice(self, text: str) -> None:
        """A warning on stderr that never lands in the middle of a streamed line."""
    def pause(self) -> None:
        """End the current streamed block so a prompt can print below it."""
        self._stream.pause()

        self._stream.pause()
        ui.emit(f"[budget] {text}", err=True, flush=True)

    def finish(self, fallback: str = "") -> None:
        if self.streamed:
            self._stream.close()
        elif fallback:
            ui.emit(fallback)


def run_repl(
    config: Config,
def _approval_hook(edit_mode: str, always: set[str], printer: "_LivePrinter") -> Any:
    """The approval callback for `llm.run_task`, or None when nothing should be
    asked: dangerous mode never asks, and with no terminal to ask on, tools
    run as they always did."""
    if edit_mode == "dangerous" or not _is_interactive():
        return None

    def ask(name: str, arguments: dict[str, Any]) -> str:
        printer.pause()
        title, target, body, is_diff = modes.describe(name, arguments)
        return ui.prompt_permission(title, target, body, is_diff)

    return modes.Approver(edit_mode, ask, always)


def _tool_hooks(edit_mode: str, printer: "_LivePrinter") -> tuple[Any, Any]:
    """`on_tool_start` / `on_tool_result` callbacks that draw tool blocks.

    The diff panel after an edit is skipped in `default` mode, where the
    approval prompt already showed it; in `auto` and `dangerous` it is the
    only place the change is shown.
    """
    current: dict[str, Any] = {}

    def start(name: str, arguments: dict[str, Any]) -> None:
        printer.pause()
        _, target, body, is_diff = modes.describe(name, arguments)
        current.update(target=target, diff=body if is_diff and edit_mode != "default" else "")
        ui.render_tool_start(name, target)

    def result(name: str, output: str) -> None:
        ui.render_tool_result(
            name, current.get("target", ""), ok=not output.startswith("Error"),
            output=output, diff=current.get("diff", ""),
        )

    return start, result


    client: Any,
    plan_mode: bool = False,
    images: list[str] | None = None,
) -> None:
    state = REPLState(
        config=config,
        client=client,
        plan_mode=plan_mode,
        pending_images=list(images or []),
        budget=budget.BudgetTracker(config.budget_tokens, config.budget_minutes),
    )
    workspace = Workspace({state.name: state}, state.name)
    state.workspace = workspace
    global _web_workspace
    _web_workspace = workspace
    ui.render_banner(__version__, config.model, config.working_dir)
    if state.plan_mode:
        ui.emit("Starting in plan mode (read-only). /build to exit.")
    if state.pending_images:
        ui.emit(f"{len(state.pending_images)} image(s) attached for your first task.")
    if config.fast_model:
        ui.emit(_handle_auto_command("", state))

    resumed = session.load(config.session_file)
    if resumed:
        state.history = resumed
        ui.emit(f"Resumed previous session ({len(resumed)} messages). /clear starts fresh.")

    prompt_session = _make_prompt_session(workspace)
    while True:
        state = workspace.state  # /session commands can change which one is active
        try:
            line = _read_line(prompt_session, state)
        except (EOFError, KeyboardInterrupt):
            ui.emit()
            return

        line = line.strip()
        if not line:
            continue
            # Ctrl+D or Ctrl+C at the prompt exits. (scripts/dev.sh stops the app
            # with Ctrl+C to restart it, so this must stay an exit.)

        dictated = False
        if line == "/voice":
            spoken = _voice_task(state.config)
            if spoken is None:
                continue
            line, dictated = spoken, True

    edit_mode: str = "default",
        # A dictated "slash clear" must run as a task, never as a command.
        if line.startswith("/") and not dictated:
            try:
                output = handle_slash_command(line, state)
            except REPLExit:
        edit_mode=edit_mode,
                return
            ui.emit(output)
            continue

        printer = _LivePrinter()
        before = None if state.plan_mode else gitutil.snapshot(state.config.working_dir)
        choice = routing.choose_model(
            line,
            state.config.model,
            state.config.fast_model if state.auto_model else "",
    elif state.edit_mode == "dangerous":
        ui.render_status("Dangerous mode: edits and shell commands run without asking.", err=False)
            images=bool(state.pending_images),
            plan_mode=state.plan_mode,
            last=state.last_model,
        )
        try:
            task_text = _with_images(line, state.pending_images, state.config, state.client)
            state.pending_images = []
            _announce_model(choice)
        on_tool_start, on_tool_result = _tool_hooks(state.edit_mode, printer)
            result = llm.run_task(
                task_text,
                state.history,
                state.client,
                choice.model,
                base_dir=state.config.working_dir,
                shell_timeout=state.config.shell_timeout,
                max_iterations=state.config.max_iterations,
                usage_file=state.config.usage_file,
                read_only=state.plan_mode,
                on_token=printer,
                on_step=_budget_hook(state.budget, printer),
                think=state.config.think_setting,
            )
        except llm.LLMError as e:
            printer.finish()
            ui.emit(f"Error: {_error_message(e, state.config)}")
            continue
        except KeyboardInterrupt:
            # Ctrl+C cancels this turn only; the history is left as it was.
            ui.stop_tool_block()
            printer.finish()
            ui.render_status("Cancelled.", err=False)
            continue

        state.history = result.history
        state.last_model = choice.model
        session.save(state.config.session_file, state.history)
        printer.finish(fallback=result.content)
        _git_report(before, line, interactive=True)


SLASH_COMMANDS = [
    "/help", "/clear", "/model", "/pull", "/usage", "/voice", "/plugins", "/mcp",
    "/budget", "/auto", "/fast", "/session", "/undo", "/image", "/plan", "/build",
    "/exit", "/quit",
]
HISTORY_FILE = os.path.join(os.path.expanduser("~"), ".forge", "history")


def _make_prompt_session(workspace: Workspace) -> Any:
    """A prompt_toolkit session (history, `/` completion, status toolbar), or
    None when stdin/stdout aren't a terminal so plain `input()` is used."""
    def status() -> str:
        state = workspace.state
        tokens, seconds = _session_totals()
        mode = "plan (read-only)" if state.plan_mode else "build"
        return (
            f"{state.config.model} · {mode} · {os.path.basename(state.config.working_dir) or '/'}"
            f" · voice {_voice_state(state.config)} · {tokens:,} tokens · {seconds / 60:.1f} min"
        )

    return ui.make_input(SLASH_COMMANDS, HISTORY_FILE, status)


def _voice_state(config: Config) -> str:
    """"on" when dictation could run (a recorder and a transcriber exist)."""
    recorder = config.voice_record.strip() or voice.find_recorder()
    transcriber = config.voice_transcribe.strip() or voice.builtin_available()
    return "on" if recorder and transcriber else "off"


def _read_line(prompt_session: Any, state: REPLState) -> str:
    return ui.read_input(prompt_session, _repl_prompt(state))


def _repl_prompt(state: REPLState) -> str:
    plan = " plan" if state.plan_mode else ""
    if state.workspace is not None and len(state.workspace.sessions) > 1:
        return f"[{state.name}{plan}] > "
    return "[plan] > " if state.plan_mode else "> "


def run_once(
    task: str,
    config: Config,
    client: Any,
    plan_mode: bool = False,
    images: list[str] | None = None,
) -> int:
    printer = _LivePrinter()
    tracker = budget.BudgetTracker(config.budget_tokens, config.budget_minutes)
    before = None if plan_mode else gitutil.snapshot(config.working_dir)
    choice = routing.choose_model(
        task, config.model, config.fast_model, images=bool(images), plan_mode=plan_mode
    )
    try:
        task_text = _with_images(task, images or [], config, client)
        _announce_model(choice, err=True)
        result = llm.run_task(
            task_text,
            llm.new_history(),
            client,
            choice.model,
            base_dir=config.working_dir,
            shell_timeout=config.shell_timeout,
            max_iterations=config.max_iterations,
            usage_file=config.usage_file,
            read_only=plan_mode,
            on_token=printer,
            on_step=_budget_hook(tracker, printer),
            think=config.think_setting,
        )
    except llm.LLMError as e:
        printer.finish()
        ui.emit(f"Error: {_error_message(e, config)}", err=True)
        return 1

    printer.finish(fallback=result.content)
    _git_report(before, task, interactive=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args, task = parse_args(argv)

    try:
        config = Config.from_env()
    except ConfigError as e:
        ui.emit(f"Error: {e}", err=True)
        return 2
    if args.model:
        config.model = args.model
        config.fast_model = ""  # an explicit --model is used as-is, no routing
    if args.host:
        config.host = args.host

    try:
        images = [vision.resolve_image(p, config.working_dir) for p in args.image]
    except vision.VisionError as e:
        ui.emit(f"Error: {e}", err=True)
        return 2

    if args.voice:
        if task is not None or args.interactive:
            ui.emit(
                "Error: --voice dictates the task, so don't also type one or pass -i "
                "(inside the REPL, use /voice).",
                err=True,
    on_tool_start, on_tool_result = _tool_hooks(edit_mode, printer)
            )
            return 2
        task = _voice_task(config, confirm=_is_interactive())
        if task is None:
            return 1

    _start_mcp(config, args.trust_mcp)
    _load_plugins(config, args.trust_plugins)
    if args.web:
        _start_web(config, args.web_port)
    if args.chat:
        _start_chat(args.chat_port)

    client = ollama.Client(host=config.host)

    if args.interactive or task is None:
        run_repl(config, client, plan_mode=plan_mode, images=images, edit_mode=edit_mode)
        return 0

    return run_once(task, config, client, plan_mode=plan_mode, images=images, edit_mode=edit_mode)

            approve=_approval_hook(edit_mode, set(), printer),
            on_tool_start=on_tool_start,
            on_tool_result=on_tool_result,

if __name__ == "__main__":
    sys.exit(main())
    edit_mode: str = "default",
    requested = {m for m in (args.mode, "plan" if args.plan else None, "dangerous" if args.dangerous_edits else None) if m}
    if len(requested) > 1:
        ui.emit(f"Error: conflicting modes requested: {', '.join(sorted(requested))}.", err=True)
        return 2
    mode = requested.pop() if requested else "default"
    plan_mode = mode == "plan"
    edit_mode = "default" if plan_mode else mode

