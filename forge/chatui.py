"""A browser chat page for forge (`forge --chat`): type a task, read the reply.

Unlike the read-only dashboard this accepts input, and a task can edit files
or run shell commands, so it is locked down the same way plus a few more:
- binds to 127.0.0.1 only;
- rejects requests whose Host isn't localhost on our port (DNS rebinding);
- POST needs a per-run secret token (given only to the page we serve), a
  JSON content type and, when the browser sends one, a same-origin Origin;
- strict Content-Security-Policy, no third-party assets, and message text is
  never treated as HTML: the Jinja template autoescapes it, and the page's
  script adds new messages with `textContent`.

The page itself is a Jinja template (`templates/chat.html`), rendered on each
load with the session's earlier messages already in it.
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .webui import HOST, _CSP

DEFAULT_PORT = 8766
MAX_BODY_BYTES = 64 * 1024

# Autoescaping is on for .html, so message text can never become markup.
_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(["html"]),
)

CHAT_CSS = """\
:root { --bg:#fafafa; --fg:#1b1b1f; --muted:#6b6b76; --card:#fff; --line:#e2e2e8; --accent:#3b5bdb; --warn:#c92a2a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141417; --fg:#ececf1; --muted:#9a9aa8; --card:#1e1e23; --line:#33333b; --accent:#748ffc; --warn:#ff8787; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; display:flex; flex-direction:column; height:100vh; }
header { display:flex; align-items:baseline; gap:1rem; padding:.75rem 1.25rem; border-bottom:1px solid var(--line); }
h1 { margin:0; font-size:1.1rem; }
#status { color:var(--muted); font-size:.85rem; }
#status.error { color:var(--warn); }
#log { flex:1; overflow:auto; list-style:none; margin:0; padding:1rem 1.25rem; display:flex; flex-direction:column; gap:.6rem; }
.msg { max-width:48rem; padding:.6rem .85rem; border:1px solid var(--line); border-radius:10px; background:var(--card); }
.msg.user { align-self:flex-end; border-color:var(--accent); }
.msg.error { border-color:var(--warn); color:var(--warn); }
.msg pre { margin:0; white-space:pre-wrap; word-break:break-word; font:inherit; }
form { display:flex; gap:.5rem; padding:.75rem 1.25rem; border-top:1px solid var(--line); }
textarea { flex:1; font:inherit; padding:.5rem .6rem; border:1px solid var(--line); border-radius:8px; background:var(--card); color:var(--fg); resize:vertical; }
button { font:inherit; padding:.5rem 1.1rem; border:0; border-radius:8px; background:var(--accent); color:#fff; cursor:pointer; }
button:disabled { opacity:.5; cursor:wait; }
"""

CHAT_JS = """\
'use strict';
const log = document.getElementById('log');
const input = document.getElementById('input');
const send = document.getElementById('send');
const status = document.getElementById('status');
const token = document.querySelector('meta[name="forge-token"]').content;

function add(role, text) {
  const li = document.createElement('li');
  li.className = 'msg ' + role;
  const pre = document.createElement('pre');
  pre.textContent = text;
  li.append(pre);
  log.append(li);
  log.scrollTop = log.scrollHeight;
}

async function submit() {
  const message = input.value.trim();
  if (!message) return;
  input.value = '';
  add('user', message);
  send.disabled = true;
  status.className = '';
  status.textContent = 'working...';
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Forge-Token': token },
      body: JSON.stringify({ message }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    add('assistant', data.reply || '(no reply)');
    status.textContent = 'ready';
  } catch (error) {
    add('error', 'Error: ' + error.message);
    status.className = 'error';
    status.textContent = 'failed';
  } finally {
    send.disabled = false;
    input.focus();
  }
}

document.getElementById('form').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
});
log.scrollTop = log.scrollHeight;
"""


def _make_handler(
    reply: Callable[[str], str],
    history: Callable[[], list[dict[str, str]]],
    port_getter: Callable[[], int],
    token: str,
) -> type:
    assets = {
        "/chat.css": ("text/css; charset=utf-8", CHAT_CSS),
        "/chat.js": ("text/javascript; charset=utf-8", CHAT_JS),
    }

    class Handler(BaseHTTPRequestHandler):
        server_version = "forge-chat"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence per-request logs
            pass

        def _send(self, status: int, content_type: str, body: bytes, extra: dict[str, str] | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", _CSP)
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            if status >= 400:
                # A refusal may leave an unread request body on a kept-alive
                # connection, which would be misparsed as the next request.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any] | list[Any]) -> None:
            self._send(status, "application/json", json.dumps(payload).encode("utf-8"))

        def _origins(self) -> set[str]:
            port = port_getter()
            return {f"http://127.0.0.1:{port}", f"http://localhost:{port}", f"http://[::1]:{port}"}

        def _host_allowed(self) -> bool:
            port = port_getter()
            return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            if not self._host_allowed():
                self._send(403, "text/plain; charset=utf-8", b"Forbidden host")
                return
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                page = _env.get_template("chat.html").render(token=token, messages=history())
                self._send(200, "text/html; charset=utf-8", page.encode("utf-8"))
            elif path in assets:
                content_type, text = assets[path]
                self._send(200, content_type, text.encode("utf-8"))
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        do_HEAD = do_GET  # noqa: N815

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            if not self._host_allowed():
                self._send(403, "text/plain; charset=utf-8", b"Forbidden host")
                return
            if self.path.split("?", 1)[0] != "/api/chat":
                self._send(404, "text/plain; charset=utf-8", b"Not found")
                return
            origin = self.headers.get("Origin")
            if origin is not None and origin not in self._origins():
                self._json(403, {"error": "Forbidden origin"})
                return
            if not hmac.compare_digest(self.headers.get("X-Forge-Token", ""), token):
                self._json(403, {"error": "Missing or wrong token"})
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                self._json(415, {"error": "Content-Type must be application/json"})
                return

            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._json(411, {"error": "Content-Length required"})
                return
            if length < 0 or length > MAX_BODY_BYTES:
                self._json(413, {"error": "Message too large"})
                return

            try:
                message = json.loads(self.rfile.read(length)).get("message", "")
            except (ValueError, AttributeError):
                self._json(400, {"error": "Body must be a JSON object with a 'message' field"})
                return
            if not isinstance(message, str) or not message.strip():
                self._json(400, {"error": "Empty message"})
                return

            try:
                answer = reply(message.strip())
            except Exception as e:  # noqa: BLE001 - a failed task must not kill the server
                self._json(500, {"error": f"{type(e).__name__}: {e}"})
                return
            self._json(200, {"reply": answer})

        def _not_allowed(self) -> None:
            self._send(405, "text/plain; charset=utf-8", b"Method not allowed", {"Allow": "GET, HEAD, POST"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed  # noqa: N815

    return Handler


class ChatServer:
    """A running chat page. `url` is what to open in a browser.

    `reply(message)` runs one task and returns the answer text; it is called
    from a server thread, so the caller must make it safe to run alongside
    the terminal REPL. `history()` returns `{role, content}` dicts to show
    when the page loads.
    """

    def __init__(
        self,
        reply: Callable[[str], str],
        history: Callable[[], list[dict[str, str]]],
        port: int = DEFAULT_PORT,
    ) -> None:
        self._holder: dict[str, int] = {}
        self.token = secrets.token_urlsafe(24)
        handler = _make_handler(reply, history, lambda: self._holder["port"], self.token)
        self.server = ThreadingHTTPServer((HOST, port), handler)  # raises OSError if the port is taken
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self._holder["port"] = self.port
        self.url = f"http://localhost:{self.port}"
        self._thread = threading.Thread(target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
