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
:root {
  --bg:#f6f7fb; --surface:#ffffff; --fg:#1c1d26; --muted:#6c6f80; --line:#e3e5ee;
  --accent:#5b5bd6; --accent-fg:#ffffff; --accent-soft:#ececfb; --warn:#c8323a; --warn-soft:#fdecee;
  --shadow:0 1px 2px rgba(20,20,40,.06), 0 4px 16px rgba(20,20,40,.06);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg:#101117; --surface:#191b24; --fg:#e9eaf2; --muted:#9295a8; --line:#292c3a;
    --accent:#7b7bf0; --accent-fg:#0d0e14; --accent-soft:#23243a; --warn:#ff8a90; --warn-soft:#2e1a1e;
    --shadow:0 1px 2px rgba(0,0,0,.4), 0 4px 16px rgba(0,0,0,.3);
  }
}
* { box-sizing: border-box; }
html, body { height:100%; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; display:flex; flex-direction:column; }

header { display:flex; align-items:center; justify-content:space-between; padding:.7rem 1.25rem; background:var(--surface); border-bottom:1px solid var(--line); }
.brand { display:flex; align-items:center; gap:.55rem; }
.logo { width:1.15rem; height:1.15rem; border-radius:.35rem; background:linear-gradient(135deg, var(--accent), #e0709a); }
h1 { margin:0; font-size:1.05rem; font-weight:650; letter-spacing:.01em; }
.pill { font-size:.78rem; color:var(--muted); background:var(--bg); border:1px solid var(--line); padding:.15rem .65rem; border-radius:999px; }
.pill.busy { color:var(--accent); border-color:var(--accent); }
.pill.error { color:var(--warn); border-color:var(--warn); }

#scroller { flex:1; overflow-y:auto; padding:1.25rem 1rem 0; scroll-behavior:smooth; }
#log { list-style:none; margin:0 auto; padding:0; max-width:46rem; display:flex; flex-direction:column; gap:1rem; }
.msg { display:flex; flex-direction:column; gap:.2rem; max-width:88%; }
.msg .who { font-size:.72rem; font-weight:600; letter-spacing:.05em; text-transform:uppercase; color:var(--muted); padding:0 .25rem; }
.msg pre { margin:0; white-space:pre-wrap; word-break:break-word; font:inherit; padding:.7rem .95rem; border-radius:14px; background:var(--surface); border:1px solid var(--line); box-shadow:var(--shadow); }
.msg.user { align-self:flex-end; align-items:flex-end; }
.msg.user pre { background:var(--accent); color:var(--accent-fg); border-color:var(--accent); border-bottom-right-radius:4px; }
.msg.assistant pre { border-bottom-left-radius:4px; }
.msg.error pre { background:var(--warn-soft); color:var(--warn); border-color:var(--warn); }

.empty { max-width:28rem; margin:18vh auto 0; text-align:center; color:var(--muted); }
.empty strong { display:block; font-size:1.15rem; color:var(--fg); margin-bottom:.35rem; }
.empty[hidden], .working[hidden] { display:none; }

.working { max-width:46rem; margin:1rem auto 0; display:flex; gap:.3rem; padding:.2rem .5rem; }
.working span { width:.5rem; height:.5rem; border-radius:50%; background:var(--accent); opacity:.35; animation:pulse 1.2s infinite ease-in-out; }
.working span:nth-child(2) { animation-delay:.15s; }
.working span:nth-child(3) { animation-delay:.3s; }
@keyframes pulse { 0%, 80%, 100% { opacity:.25; transform:scale(.85); } 40% { opacity:1; transform:scale(1); } }
@media (prefers-reduced-motion: reduce) { .working span { animation:none; opacity:.7; } #scroller { scroll-behavior:auto; } }

form { padding:.75rem 1rem 1rem; }
.composer { max-width:46rem; margin:0 auto; display:flex; align-items:flex-end; gap:.5rem; padding:.45rem .45rem .45rem .95rem; background:var(--surface); border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); }
.composer:focus-within { border-color:var(--accent); }
textarea { flex:1; font:inherit; color:var(--fg); background:transparent; border:0; outline:0; resize:none; max-height:12rem; padding:.4rem 0; }
textarea::placeholder { color:var(--muted); }
button { flex:none; width:2.25rem; height:2.25rem; border:0; border-radius:50%; background:var(--accent); color:var(--accent-fg); font-size:1.15rem; line-height:1; cursor:pointer; }
button:hover:not(:disabled) { filter:brightness(1.08); }
button:disabled { opacity:.4; cursor:wait; }
button:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.hint { max-width:46rem; margin:.4rem auto 0; text-align:center; font-size:.75rem; color:var(--muted); }
"""

CHAT_JS = """\
'use strict';
const scroller = document.getElementById('scroller');
const log = document.getElementById('log');
const empty = document.getElementById('empty');
const working = document.getElementById('working');
const input = document.getElementById('input');
const send = document.getElementById('send');
const status = document.getElementById('status');
const token = document.querySelector('meta[name="forge-token"]').content;

function scrollDown() { scroller.scrollTop = scroller.scrollHeight; }

function add(role, text) {
  const li = document.createElement('li');
  li.className = 'msg ' + role;
  const who = document.createElement('span');
  who.className = 'who';
  who.textContent = role === 'user' ? 'You' : role === 'error' ? 'Error' : 'forge';
  const pre = document.createElement('pre');
  pre.textContent = text;
  li.append(who, pre);
  log.append(li);
  empty.hidden = true;
  scrollDown();
}

function setBusy(busy) {
  send.disabled = busy;
  working.hidden = !busy;
  status.className = busy ? 'pill busy' : 'pill';
  status.textContent = busy ? 'working...' : 'ready';
  if (busy) scrollDown();
}

function autosize() {
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 192) + 'px';
}

async function submit() {
  const message = input.value.trim();
  if (!message || send.disabled) return;
  input.value = '';
  autosize();
  add('user', message);
  setBusy(true);
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Forge-Token': token },
      body: JSON.stringify({ message }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'HTTP ' + response.status);
    add('assistant', data.reply || '(no reply)');
    setBusy(false);
  } catch (error) {
    add('error', error.message);
    setBusy(false);
    status.className = 'pill error';
    status.textContent = 'failed';
  } finally {
    input.focus();
  }
}

document.getElementById('form').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
input.addEventListener('input', autosize);
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
});
scrollDown();
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
