"""A small Language Server Protocol client, so the model can ask real
semantic questions (definition, references, hover, diagnostics) instead of
grepping text.

`LSPClient` speaks JSON-RPC over a language server's stdio. `get_client()`
lazily starts one server per project directory and reuses it for the rest
of the process. Only Python (via pyright) is wired up today; the client
itself is server-agnostic.

Everything that can fail raises `LSPError`, which the tool layer turns
into an ordinary "Error: ..." result for the model.
"""

from __future__ import annotations

import atexit
import itertools
import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

DEFAULT_SERVER = ["pyright-langserver", "--stdio"]
PYTHON_EXTENSIONS = {".py", ".pyi"}
REQUEST_TIMEOUT = 30.0
DIAGNOSTICS_TIMEOUT = 20.0

SEVERITIES = {1: "error", 2: "warning", 3: "info", 4: "hint"}


class LSPError(Exception):
    """The language server is unavailable, timed out, or returned an error."""


# ---------------------------------------------------------------------------
# Wire format
# ---------------------------------------------------------------------------


def _frame(message: dict[str, Any]) -> bytes:
    body = json.dumps(message).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def _read_message(stream: Any) -> dict[str, Any] | None:
    """Read one framed message, or None at end of stream."""
    length = None
    while True:
        line = stream.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        name, _, value = line.decode("ascii", "replace").partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
    if length is None:
        return None
    body = stream.read(length)
    if len(body) < length:
        return None
    return json.loads(body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class LSPClient:
    def __init__(self, command: list[str], root: str) -> None:
        self.command = command
        self.root = root
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diag_events: dict[str, threading.Event] = {}
        self._texts: dict[str, str] = {}
        self._versions: dict[str, int] = {}

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.root,
            )
        except OSError as e:
            raise LSPError(f"Could not start language server {self.command[0]!r}: {e}") from e

        threading.Thread(target=self._read_loop, daemon=True).start()

        self.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": Path(self.root).as_uri(),
                "workspaceFolders": [{"uri": Path(self.root).as_uri(), "name": "project"}],
                "capabilities": {
                    "textDocument": {
                        "synchronization": {"didSave": False},
                        "publishDiagnostics": {},
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "definition": {"linkSupport": True},
                        "references": {},
                    },
                    "workspace": {"configuration": True, "workspaceFolders": True},
                },
            },
        )
        self.notify("initialized", {})

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def shutdown(self) -> None:
        if self._proc is None:
            return
        try:
            if self.alive:
                self.request("shutdown", None, timeout=3)
                self.notify("exit", None)
        except LSPError:
            pass
        finally:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=3)
            except (OSError, subprocess.SubprocessError):
                self._proc.kill()

    # -- JSON-RPC plumbing --------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        if not self.alive or self._proc is None or self._proc.stdin is None:
            raise LSPError("Language server is not running")
        try:
            with self._write_lock:
                self._proc.stdin.write(_frame(message))
                self._proc.stdin.flush()
        except OSError as e:
            raise LSPError(f"Lost connection to language server: {e}") from e

    def notify(self, method: str, params: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Any, timeout: float = REQUEST_TIMEOUT) -> Any:
        request_id = next(self._ids)
        reply: queue.Queue = queue.Queue(maxsize=1)
        with self._state_lock:
            self._pending[request_id] = reply
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            try:
                response = reply.get(timeout=timeout)
            except queue.Empty:
                raise LSPError(f"Language server timed out on {method} after {timeout:.0f}s")
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

        if "error" in response:
            error = response["error"]
            message = error.get("message", error) if isinstance(error, dict) else error
            raise LSPError(f"Language server error on {method}: {message}")
        return response.get("result")

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                message = _read_message(self._proc.stdout)
                if message is None:
                    break
                self._dispatch(message)
        except (OSError, ValueError):
            pass
        finally:
            # Server is gone: wake everything still waiting.
            with self._state_lock:
                waiting = list(self._pending.values())
                events = list(self._diag_events.values())
            for reply in waiting:
                try:
                    reply.put_nowait({"error": {"message": "language server exited"}})
                except queue.Full:
                    pass
            for event in events:
                event.set()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "method" in message and "id" in message:
            self._answer_server_request(message)
        elif "id" in message:
            with self._state_lock:
                reply = self._pending.get(message["id"])
            if reply is not None:
                try:
                    reply.put_nowait(message)
                except queue.Full:
                    pass
        elif message.get("method") == "textDocument/publishDiagnostics":
            params = message.get("params") or {}
            uri = params.get("uri")
            if uri:
                with self._state_lock:
                    self._diagnostics[uri] = params.get("diagnostics") or []
                    event = self._diag_events.setdefault(uri, threading.Event())
                event.set()

    def _answer_server_request(self, message: dict[str, Any]) -> None:
        """Servers ask the client things too (settings, capability
        registration, progress). Answer minimally so they never block."""
        params = message.get("params") or {}
        if message["method"] == "workspace/configuration":
            result: Any = [{} for _ in params.get("items", [])]
        else:
            result = None
        try:
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
        except LSPError:
            pass

    # -- documents ----------------------------------------------------------

    def sync_file(self, path: str) -> str:
        """Make the server's copy of `path` match what's on disk; returns its URI."""
        uri = Path(path).as_uri()
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            raise LSPError(f"Could not read {path!r}: {e}") from e

        with self._state_lock:
            known = self._texts.get(uri)
            if known == text:
                return uri
            version = self._versions.get(uri, 0) + 1
            self._versions[uri] = version
            self._texts[uri] = text
            self._diag_events.setdefault(uri, threading.Event()).clear()

        if known is None:
            self.notify(
                "textDocument/didOpen",
                {"textDocument": {"uri": uri, "languageId": "python", "version": version, "text": text}},
            )
        else:
            self.notify(
                "textDocument/didChange",
                {
                    "textDocument": {"uri": uri, "version": version},
                    "contentChanges": [{"text": text}],
                },
            )
        return uri

    def _position_request(self, method: str, path: str, line: int, column: int, **extra: Any) -> Any:
        uri = self.sync_file(path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": column - 1},
            **extra,
        }
        return self.request(method, params)

    def definition(self, path: str, line: int, column: int) -> list[dict[str, Any]]:
        return _locations(self._position_request("textDocument/definition", path, line, column))

    def references(self, path: str, line: int, column: int) -> list[dict[str, Any]]:
        result = self._position_request(
            "textDocument/references", path, line, column, context={"includeDeclaration": True}
        )
        return _locations(result)

    def hover(self, path: str, line: int, column: int) -> str:
        result = self._position_request("textDocument/hover", path, line, column)
        return _hover_text((result or {}).get("contents"))

    def diagnostics(self, path: str) -> list[dict[str, Any]]:
        uri = self.sync_file(path)
        with self._state_lock:
            event = self._diag_events.setdefault(uri, threading.Event())
        if not event.wait(DIAGNOSTICS_TIMEOUT):
            raise LSPError(f"Language server did not report diagnostics within {DIAGNOSTICS_TIMEOUT:.0f}s")
        if not self.alive:
            raise LSPError("Language server exited")
        with self._state_lock:
            return list(self._diagnostics.get(uri, []))


# ---------------------------------------------------------------------------
# Result normalization
# ---------------------------------------------------------------------------


def _locations(result: Any) -> list[dict[str, Any]]:
    """Location | Location[] | LocationLink[] | None -> [{uri, line, column}] (1-based)."""
    if not result:
        return []
    items = result if isinstance(result, list) else [result]
    found = []
    for item in items:
        uri = item.get("uri") or item.get("targetUri")
        rng = item.get("range") or item.get("targetSelectionRange")
        if not uri or not rng:
            continue
        start = rng["start"]
        found.append({"uri": uri, "line": start["line"] + 1, "column": start["character"] + 1})
    return found


def _hover_text(contents: Any) -> str:
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents.strip()
    if isinstance(contents, dict):
        return str(contents.get("value", "")).strip()
    if isinstance(contents, list):
        return "\n\n".join(t for t in (_hover_text(c) for c in contents) if t)
    return ""


def uri_to_path(uri: str) -> str:
    return unquote(urlparse(uri).path)


# ---------------------------------------------------------------------------
# Per-project client management
# ---------------------------------------------------------------------------

_clients: dict[str, LSPClient] = {}
_clients_lock = threading.Lock()


def server_command() -> list[str]:
    """The server to launch: `FORGE_LSP_COMMAND` if set, else pyright.

    Also looks next to the running Python, so a pyright installed in the
    project's venv is found even when the venv isn't activated.
    """
    override = os.environ.get("FORGE_LSP_COMMAND")
    command = shlex.split(override) if override else list(DEFAULT_SERVER)

    if shutil.which(command[0]) is None:
        sibling = os.path.join(os.path.dirname(sys.executable), command[0])
        if os.path.isfile(sibling):
            command[0] = sibling
        else:
            raise LSPError(
                f"Language server {command[0]!r} not found. Install it with "
                f"`pip install pyright` (or set FORGE_LSP_COMMAND)."
            )
    return command


def get_client(base_dir: str) -> LSPClient:
    root = os.path.realpath(base_dir)
    with _clients_lock:
        client = _clients.get(root)
        if client is not None and client.alive:
            return client
        client = LSPClient(server_command(), root)
        try:
            client.start()
        except LSPError:
            client.shutdown()  # don't leave a half-started server running
            raise
        _clients[root] = client
        return client


def shutdown_all() -> None:
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        client.shutdown()


atexit.register(shutdown_all)
