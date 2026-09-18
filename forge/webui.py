"""A minimal read-only web dashboard (`forge --web`): usage stats and the
conversation history of each open session, viewable in a browser without
leaving the terminal focused.

Deliberately small and locked down, since it exposes your conversations:
- binds to 127.0.0.1 only;
- GET only (every other method gets 405), no endpoint changes anything;
- rejects requests whose Host header isn't localhost on our port, which
  blocks DNS-rebinding attacks from web pages;
- strict Content-Security-Policy, no third-party assets, and the page
  renders everything with `textContent` (never as HTML).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

DEFAULT_PORT = 8765
HOST = "127.0.0.1"

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>forge dashboard</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header>
  <h1>forge</h1>
  <span id="status">connecting...</span>
</header>
<main>
  <section id="usage" class="cards" aria-label="Usage"></section>
  <section aria-label="Sessions">
    <div id="tabs" role="tablist"></div>
    <div id="session-info"></div>
    <ol id="messages"></ol>
    <h2>Recent file changes</h2>
    <ul id="changes"></ul>
  </section>
</main>
<script src="/app.js"></script>
</body>
</html>
"""

STYLE_CSS = """\
:root { --bg:#fafafa; --fg:#1b1b1f; --muted:#6b6b76; --card:#fff; --line:#e2e2e8; --accent:#3b5bdb; --warn:#c92a2a; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141417; --fg:#ececf1; --muted:#9a9aa8; --card:#1e1e23; --line:#33333b; --accent:#748ffc; --warn:#ff8787; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.5 system-ui, sans-serif; }
header { display:flex; align-items:baseline; gap:1rem; padding:1rem 1.25rem; border-bottom:1px solid var(--line); }
h1 { margin:0; font-size:1.2rem; }
h2 { font-size:1rem; margin:1.5rem 0 .5rem; }
#status { color:var(--muted); font-size:.85rem; }
#status.error { color:var(--warn); }
main { padding:1rem 1.25rem; max-width:64rem; margin:0 auto; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(11rem,1fr)); gap:.75rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:.75rem 1rem; }
.card .label { color:var(--muted); font-size:.78rem; text-transform:uppercase; letter-spacing:.04em; }
.card .value { font-size:1.35rem; font-weight:600; }
.card .sub { color:var(--muted); font-size:.8rem; }
.bar { height:6px; background:var(--line); border-radius:3px; margin-top:.4rem; overflow:hidden; }
.bar > span { display:block; height:100%; background:var(--accent); }
.bar.over > span { background:var(--warn); }
#tabs { display:flex; flex-wrap:wrap; gap:.4rem; margin:1.25rem 0 .5rem; }
#tabs button { font:inherit; padding:.3rem .8rem; border:1px solid var(--line); border-radius:999px; background:var(--card); color:var(--fg); cursor:pointer; }
#tabs button[aria-selected="true"] { border-color:var(--accent); color:var(--accent); font-weight:600; }
#session-info { color:var(--muted); font-size:.85rem; margin-bottom:.75rem; word-break:break-all; }
#messages { list-style:none; margin:0; padding:0; display:flex; flex-direction:column; gap:.5rem; }
.msg { background:var(--card); border:1px solid var(--line); border-radius:8px; padding:.5rem .75rem; }
.msg .role { font-size:.72rem; font-weight:700; text-transform:uppercase; letter-spacing:.05em; color:var(--accent); }
.msg.user .role { color:var(--fg); }
.msg.tool .role { color:var(--muted); }
.msg pre { margin:.25rem 0 0; white-space:pre-wrap; word-break:break-word; font:inherit; max-height:22rem; overflow:auto; }
.msg .calls { color:var(--muted); font-size:.8rem; }
.empty { color:var(--muted); }
#changes { margin:0; padding-left:1.2rem; color:var(--muted); }
"""

APP_JS = """\
'use strict';
let selected = null;
let lastState = null;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
const fmt = (n) => Number(n).toLocaleString();

function card(label, value, sub, ratio) {
  const c = el('div', 'card');
  c.append(el('div', 'label', label), el('div', 'value', value));
  if (sub) c.append(el('div', 'sub', sub));
  if (ratio !== null && ratio !== undefined) {
    const bar = el('div', ratio > 1 ? 'bar over' : 'bar');
    const fill = el('span');
    fill.style.width = Math.min(100, ratio * 100) + '%';
    bar.append(fill);
    c.append(bar);
  }
  return c;
}

function renderUsage(state) {
  const u = state.usage, b = state.budget;
  const tokens = u.session.prompt_tokens + u.session.completion_tokens;
  const minutes = u.compute_seconds / 60;
  const box = document.getElementById('usage');
  box.replaceChildren(
    card('Session tokens', fmt(tokens),
      fmt(u.session.calls) + ' calls (' + fmt(u.session.prompt_tokens) + ' in / ' + fmt(u.session.completion_tokens) + ' out)',
      b.tokens_limit > 0 ? tokens / b.tokens_limit : null),
    card('Model compute', minutes.toFixed(1) + ' min', b.minutes_limit > 0 ? 'soft limit ' + b.minutes_limit + ' min' : 'no limit',
      b.minutes_limit > 0 ? minutes / b.minutes_limit : null),
    card('All-time tokens', fmt(u.all_time.prompt_tokens + u.all_time.completion_tokens), fmt(u.all_time.calls) + ' calls'),
    card('Estimated saved', '$' + u.estimated_saved_usd.toFixed(4), 'vs. hosted inference')
  );
}

function renderSessions(state) {
  const tabs = document.getElementById('tabs');
  const names = state.sessions.map((s) => s.name);
  if (!selected || !names.includes(selected)) {
    const current = state.sessions.find((s) => s.current);
    selected = current ? current.name : names[0];
  }
  tabs.replaceChildren(...state.sessions.map((s) => {
    const b = el('button', '', s.name + (s.current ? ' *' : ''));
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(s.name === selected));
    b.addEventListener('click', () => { selected = s.name; render(); });
    return b;
  }));

  const session = state.sessions.find((s) => s.name === selected);
  const info = document.getElementById('session-info');
  const list = document.getElementById('messages');
  const changes = document.getElementById('changes');
  if (!session) {
    info.textContent = 'No session yet.';
    list.replaceChildren(); changes.replaceChildren();
    return;
  }
  info.textContent = session.directory + ' - ' + session.model + (session.plan_mode ? ' - plan mode' : '') +
    ' - ' + session.message_count + ' messages' + (session.message_count > session.messages.length ? ' (showing the latest ' + session.messages.length + ')' : '');

  if (session.messages.length === 0) {
    list.replaceChildren(el('li', 'empty', 'No messages yet.'));
  } else {
    list.replaceChildren(...session.messages.map((m) => {
      const li = el('li', 'msg ' + m.role);
      li.append(el('div', 'role', m.role + (m.name ? ' (' + m.name + ')' : '')));
      if (m.tool_calls && m.tool_calls.length) li.append(el('div', 'calls', 'calls: ' + m.tool_calls.join(', ')));
      if (m.content) li.append(el('pre', '', m.content));
      return li;
    }));
  }
  changes.replaceChildren(...(session.changes.length ? session.changes.map((c) => el('li', '', c)) : [el('li', 'empty', 'None this session.')]));
}

function render() {
  if (!lastState) return;
  renderUsage(lastState);
  renderSessions(lastState);
}

async function refresh() {
  const status = document.getElementById('status');
  try {
    const response = await fetch('/api/state', { cache: 'no-store' });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    lastState = await response.json();
    render();
    status.textContent = 'updated ' + new Date().toLocaleTimeString();
    status.className = '';
  } catch (error) {
    status.textContent = 'disconnected (' + error.message + ')';
    status.className = 'error';
  }
}

refresh();
setInterval(refresh, 2000);
"""

_ASSETS = {
    "/": ("text/html; charset=utf-8", INDEX_HTML),
    "/index.html": ("text/html; charset=utf-8", INDEX_HTML),
    "/style.css": ("text/css; charset=utf-8", STYLE_CSS),
    "/app.js": ("text/javascript; charset=utf-8", APP_JS),
}

_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def _make_handler(snapshot: Callable[[], dict[str, Any]], port_getter: Callable[[], int]) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "forge-dashboard"
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
                # We never read a request body on a refusal; on a kept-alive
                # connection those bytes would be misparsed as the next request.
                self.send_header("Connection", "close")
                self.close_connection = True
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _host_allowed(self) -> bool:
            port = port_getter()
            return self.headers.get("Host", "") in {
                f"127.0.0.1:{port}",
                f"localhost:{port}",
                f"[::1]:{port}",
            }

        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            if not self._host_allowed():
                self._send(403, "text/plain; charset=utf-8", b"Forbidden host")
                return

            path = self.path.split("?", 1)[0]
            if path in _ASSETS:
                content_type, text = _ASSETS[path]
                self._send(200, content_type, text.encode("utf-8"))
            elif path == "/api/state":
                try:
                    payload = json.dumps(snapshot()).encode("utf-8")
                    self._send(200, "application/json", payload)
                except Exception as e:  # noqa: BLE001 - a bad snapshot must not kill the server
                    error = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode("utf-8")
                    self._send(500, "application/json", error)
            else:
                self._send(404, "text/plain; charset=utf-8", b"Not found")

        do_HEAD = do_GET  # noqa: N815

        def _read_only(self) -> None:
            self._send(405, "text/plain; charset=utf-8", b"Read-only dashboard", {"Allow": "GET, HEAD"})

        do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _read_only  # noqa: N815

    return Handler


class Dashboard:
    """A running dashboard server. `url` is what to open in a browser."""

    def __init__(self, snapshot: Callable[[], dict[str, Any]], port: int = DEFAULT_PORT) -> None:
        self._holder: dict[str, int] = {}
        handler = _make_handler(snapshot, lambda: self._holder["port"])
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
