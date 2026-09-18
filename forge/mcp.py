"""Model Context Protocol (MCP) client: connect forge to external tool
servers (filesystem, GitHub, databases, ...) and expose their tools to the
model next to the built-in ones.

Only the stdio transport is supported: forge launches each configured
server as a subprocess and speaks newline-delimited JSON-RPC to it.

Server tools appear to the model as `mcp__<server>__<tool>`. Because an
MCP server is an arbitrary program, a project's `forge.toml` can't start
one without the user's approval (see `fingerprint`/`is_trusted`), and in
plan mode only tools the server marks `readOnlyHint` are offered.
"""

from __future__ import annotations

import atexit
import hashlib
import itertools
import json
import os
import queue
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Any

from .config import MCPServerConfig

PROTOCOL_VERSION = "2025-06-18"
MAX_RESULT_CHARS = 20_000
TRUST_FILE = os.path.join(os.path.expanduser("~"), ".forge", "mcp_trust.json")

_INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")


class MCPError(Exception):
    """A server couldn't start, timed out, or returned an error."""


# ---------------------------------------------------------------------------
# Trust: a repo's forge.toml must not be able to run programs unasked
# ---------------------------------------------------------------------------


def fingerprint(servers: dict[str, MCPServerConfig]) -> str:
    """Stable hash of the server definitions (commands, args, env)."""
    canonical = json.dumps(
        {
            name: {"command": s.command, "args": s.args, "env": s.env}
            for name, s in sorted(servers.items())
        },
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _load_trust() -> dict[str, str]:
    try:
        with open(TRUST_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def is_trusted(project_dir: str, servers: dict[str, MCPServerConfig]) -> bool:
    return _load_trust().get(os.path.realpath(project_dir)) == fingerprint(servers)


def trust(project_dir: str, servers: dict[str, MCPServerConfig]) -> None:
    """Remember approval for exactly these definitions in this project;
    editing them invalidates it. Best-effort: failing to save just means
    the user is asked again next time."""
    data = _load_trust()
    data[os.path.realpath(project_dir)] = fingerprint(servers)
    try:
        os.makedirs(os.path.dirname(TRUST_FILE), exist_ok=True)
        with open(TRUST_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Client for one server
# ---------------------------------------------------------------------------


class MCPClient:
    def __init__(self, name: str, config: MCPServerConfig, cwd: str) -> None:
        self.name = name
        self.config = config
        self.cwd = cwd
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        env = {**os.environ, **{k: os.path.expandvars(v) for k, v in self.config.env.items()}}
        args = [os.path.expandvars(a) for a in self.config.args]
        try:
            self._proc = subprocess.Popen(
                [self.config.command, *args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.cwd,
                env=env,
            )
        except OSError as e:
            raise MCPError(f"could not start {self.config.command!r}: {e}") from e

        threading.Thread(target=self._read_loop, daemon=True).start()
        try:
            self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "forge", "version": "0.1.0"},
                },
            )
            self.notify("notifications/initialized")
        except MCPError:
            self.shutdown()
            raise

    def shutdown(self) -> None:
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()  # the polite way to stop a stdio server
            proc.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                proc.kill()

    # -- JSON-RPC ----------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise MCPError("server is not running")
        data = json.dumps(message).encode("utf-8") + b"\n"
        try:
            with self._write_lock:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
        except OSError as e:
            raise MCPError(f"lost connection to server: {e}") from e

    def notify(self, method: str, params: Any = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)

    def request(self, method: str, params: Any = None) -> Any:
        request_id = next(self._ids)
        reply: queue.Queue = queue.Queue(maxsize=1)
        with self._lock:
            self._pending[request_id] = reply
        try:
            message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                message["params"] = params
            self._send(message)
            try:
                response = reply.get(timeout=self.config.timeout)
            except queue.Empty:
                raise MCPError(f"{method} timed out after {self.config.timeout:g}s")
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

        if "error" in response:
            error = response["error"]
            message_text = error.get("message", error) if isinstance(error, dict) else error
            raise MCPError(f"{method} failed: {message_text}")
        return response.get("result")

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for raw in self._proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # stray non-protocol output
                if isinstance(message, dict):
                    self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                waiting = list(self._pending.values())
            for reply in waiting:
                try:
                    reply.put_nowait({"error": {"message": "server exited"}})
                except queue.Full:
                    pass

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._answer_server_request(message)
        elif "id" in message:
            with self._lock:
                reply = self._pending.get(message["id"])
            if reply is not None:
                try:
                    reply.put_nowait(message)
                except queue.Full:
                    pass
        # Notifications (progress, logging, list_changed) are ignored.

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        if message["method"] == "ping":
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": {}}
        else:  # sampling, roots, elicitation: forge advertised none of them
            reply = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "Method not found"},
            }
        try:
            self._send(reply)
        except MCPError:
            pass

    # -- MCP methods -------------------------------------------------------

    def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(100):  # bounded: a server can't loop us forever
            result = self.request("tools/list", {"cursor": cursor} if cursor else None) or {}
            tools.extend(t for t in result.get("tools", []) if isinstance(t, dict) and t.get("name"))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request("tools/call", {"name": name, "arguments": arguments}) or {}


# ---------------------------------------------------------------------------
# Result formatting
# ---------------------------------------------------------------------------


def format_result(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource":
            resource = item.get("resource") or {}
            parts.append(str(resource.get("text") or f"[resource: {resource.get('uri', '?')}]"))
        elif kind == "resource_link":
            parts.append(f"[resource link: {item.get('uri', '?')}]")
        else:  # image, audio: the model can't use the bytes
            parts.append(f"[{kind or 'unknown'} content: {item.get('mimeType', 'unknown type')}]")

    text = "\n".join(p for p in parts if p)
    if not text and result.get("structuredContent") is not None:
        text = json.dumps(result["structuredContent"], indent=2)
    if not text:
        text = "(no output)"
    if len(text) > MAX_RESULT_CHARS:
        text = text[:MAX_RESULT_CHARS] + f"\n... [truncated, {len(text) - MAX_RESULT_CHARS} more characters]"
    return f"Error: {text}" if result.get("isError") else text


# ---------------------------------------------------------------------------
# All configured servers
# ---------------------------------------------------------------------------


@dataclass
class MCPTool:
    qualified: str  # the name the model sees
    server: str
    name: str  # the server's own name for it
    description: str
    schema: dict[str, Any]
    read_only: bool


def _qualified_name(server: str, tool: str, taken: set[str]) -> str:
    base = _INVALID_NAME_CHARS.sub("_", f"mcp__{server}__{tool}")[:64]
    name, n = base, 1
    while name in taken:  # sanitizing or truncating can collide
        n += 1
        suffix = f"_{n}"
        name = base[: 64 - len(suffix)] + suffix
    return name


class MCPManager:
    def __init__(self) -> None:
        self.clients: dict[str, MCPClient] = {}
        self.tools: dict[str, MCPTool] = {}
        self.errors: dict[str, str] = {}

    def start(self, servers: dict[str, MCPServerConfig], cwd: str) -> None:
        """Connect to every server; one failing never stops the others."""
        for name, config in servers.items():
            client = MCPClient(name, config, cwd)
            try:
                client.start()
                listed = client.list_tools()
            except MCPError as e:
                client.shutdown()
                self.errors[name] = str(e)
                continue
            self.clients[name] = client
            for tool in listed:
                schema = tool.get("inputSchema")
                if not isinstance(schema, dict) or schema.get("type") != "object":
                    schema = {"type": "object", "properties": {}}
                annotations = tool.get("annotations") or {}
                qualified = _qualified_name(name, tool["name"], set(self.tools))
                self.tools[qualified] = MCPTool(
                    qualified=qualified,
                    server=name,
                    name=tool["name"],
                    description=str(tool.get("description") or tool.get("title") or "").strip(),
                    schema=schema,
                    read_only=annotations.get("readOnlyHint") is True,
                )

    def has_tool(self, qualified: str) -> bool:
        return qualified in self.tools

    def is_read_only(self, qualified: str) -> bool:
        tool = self.tools.get(qualified)
        return bool(tool and tool.read_only)

    def schemas(self, read_only: bool = False) -> list[dict[str, Any]]:
        """Function schemas for the model. Plan mode gets only tools the
        server itself marks read-only."""
        schemas = []
        for tool in self.tools.values():
            if read_only and not tool.read_only:
                continue
            description = f"[MCP server: {tool.server}] {tool.description}".strip()[:1000]
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.qualified,
                        "description": description,
                        "parameters": tool.schema,
                    },
                }
            )
        return schemas

    def call(self, qualified: str, arguments: dict[str, Any]) -> str:
        tool = self.tools.get(qualified)
        if tool is None:
            raise MCPError(f"unknown MCP tool {qualified!r}")
        client = self.clients[tool.server]
        if not client.alive:
            raise MCPError(f"MCP server {tool.server!r} has exited")
        return format_result(client.call_tool(tool.name, arguments))

    def describe(self) -> str:
        if not self.clients and not self.errors:
            return "No MCP servers connected."
        lines = []
        for name in self.clients:
            tools = [t for t in self.tools.values() if t.server == name]
            state = "" if self.clients[name].alive else " (exited)"
            lines.append(f"{name}{state}: {len(tools)} tool(s)")
            lines.extend(f"  {t.qualified}" + (" [read-only]" if t.read_only else "") for t in tools)
        lines.extend(f"{name}: failed to start: {error}" for name, error in self.errors.items())
        return "\n".join(lines)

    def shutdown(self) -> None:
        for client in self.clients.values():
            client.shutdown()
        self.clients.clear()
        self.tools.clear()


_manager: MCPManager | None = None


def set_manager(manager: MCPManager | None) -> None:
    global _manager
    old, _manager = _manager, manager
    if old is not None and old is not manager:
        old.shutdown()


def get_manager() -> MCPManager | None:
    return _manager


atexit.register(lambda: set_manager(None))
