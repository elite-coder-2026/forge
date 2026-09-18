import http.client
import json
import os
import shutil
import subprocess
import tempfile

import pytest

from forge import llm, main, session, undo, webui
from forge.config import Config


@pytest.fixture(autouse=True)
def _reset(monkeypatch, tmp_path):
    monkeypatch.setattr(session, "SESSIONS_DIR", str(tmp_path / "sessions"))
    main._web_workspace = None
    llm.reset_usage()
    undo.clear()
    yield
    main._web_workspace = None
    llm.reset_usage()
    undo.clear()


class Counter:
    def __init__(self, payload=None, error=None):
        self.calls = 0
        self.payload = payload if payload is not None else {"hello": "world"}
        self.error = error

    def __call__(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.payload


@pytest.fixture
def make_dashboard():
    started = []

    def make(snapshot):
        dashboard = webui.Dashboard(snapshot, port=0)
        started.append(dashboard)
        return dashboard

    yield make
    for dashboard in started:
        try:
            dashboard.stop()
        except Exception:
            pass


def request(dashboard, path="/", method="GET", host=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", dashboard.port, timeout=5)
    headers = {"Host": host} if host is not None else {}
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


# --- serving ----------------------------------------------------------------


def test_binds_to_localhost_only(make_dashboard):
    dashboard = make_dashboard(Counter())
    assert dashboard.server.server_address[0] == "127.0.0.1"
    assert dashboard.url == f"http://localhost:{dashboard.port}"


def test_serves_the_page_and_its_assets(make_dashboard):
    dashboard = make_dashboard(Counter())
    page, html = request(dashboard, "/")
    assert page.status == 200 and page.getheader("Content-Type").startswith("text/html")
    assert b'src="/app.js"' in html and b'href="/style.css"' in html
    css, _ = request(dashboard, "/style.css")
    js, body = request(dashboard, "/app.js")
    assert css.getheader("Content-Type").startswith("text/css")
    assert js.getheader("Content-Type").startswith("text/javascript") and b"/api/state" in body


def test_every_response_carries_the_security_headers(make_dashboard):
    dashboard = make_dashboard(Counter())
    for path in ("/", "/app.js", "/style.css", "/api/state", "/missing"):
        response, _ = request(dashboard, path)
        csp = response.getheader("Content-Security-Policy")
        assert "default-src 'none'" in csp and "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert response.getheader("X-Content-Type-Options") == "nosniff"
        assert response.getheader("Cache-Control") == "no-store"


def test_api_state_returns_a_fresh_snapshot_each_time(make_dashboard):
    snapshot = Counter({"a": 1})
    dashboard = make_dashboard(snapshot)
    response, body = request(dashboard, "/api/state")
    assert response.status == 200 and response.getheader("Content-Type") == "application/json"
    assert json.loads(body) == {"a": 1}
    request(dashboard, "/api/state?cache=bust")  # query strings are ignored
    assert snapshot.calls == 2


def test_unknown_paths_are_404_including_traversal(make_dashboard):
    dashboard = make_dashboard(Counter())
    for path in ("/nope", "/api", "/../etc/passwd", "/%2e%2e/etc/passwd", "/app.js/extra"):
        assert request(dashboard, path)[0].status == 404


def test_head_has_headers_but_no_body(make_dashboard):
    dashboard = make_dashboard(Counter())
    response, body = request(dashboard, "/", method="HEAD")
    assert response.status == 200 and body == b"" and int(response.getheader("Content-Length")) > 0


# --- read-only --------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
def test_every_write_method_is_refused_without_touching_state(make_dashboard, method):
    snapshot = Counter()
    dashboard = make_dashboard(snapshot)
    for path in ("/", "/api/state"):
        response, _ = request(dashboard, path, method=method, body=b"x=1" if method != "OPTIONS" else None)
        assert response.status == 405 and response.getheader("Allow") == "GET, HEAD"
    assert snapshot.calls == 0


# --- DNS rebinding ----------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/api/state"])
def test_foreign_host_headers_are_rejected(make_dashboard, path):
    snapshot = Counter()
    dashboard = make_dashboard(snapshot)
    for host in ("evil.example.com", f"evil.example.com:{dashboard.port}", "localhost", f"localhost:{dashboard.port + 1}", ""):
        assert request(dashboard, path, host=host)[0].status == 403
    assert snapshot.calls == 0


def test_localhost_hosts_are_accepted(make_dashboard):
    dashboard = make_dashboard(Counter())
    for host in (f"localhost:{dashboard.port}", f"127.0.0.1:{dashboard.port}", f"[::1]:{dashboard.port}"):
        assert request(dashboard, "/api/state", host=host)[0].status == 200


# --- robustness -------------------------------------------------------------


def test_a_failing_snapshot_is_a_500_and_the_server_survives(make_dashboard):
    snapshot = Counter(error=RuntimeError("bad state"))
    dashboard = make_dashboard(snapshot)
    response, body = request(dashboard, "/api/state")
    assert response.status == 500 and "bad state" in json.loads(body)["error"]
    assert request(dashboard, "/")[0].status == 200


def test_port_in_use_raises_oserror(make_dashboard):
    first = make_dashboard(Counter())
    with pytest.raises(OSError):
        webui.Dashboard(Counter(), port=first.port)


def test_stop_closes_the_server(make_dashboard):
    dashboard = make_dashboard(Counter())
    dashboard.stop()
    with pytest.raises(OSError):
        request(dashboard, "/")


# --- the page itself --------------------------------------------------------


def test_page_never_renders_data_as_html():
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert forbidden not in webui.APP_JS
    assert "textContent" in webui.APP_JS


def test_page_loads_only_its_own_assets():
    assert "http://" not in webui.INDEX_HTML and "https://" not in webui.INDEX_HTML
    assert "<style" not in webui.INDEX_HTML and "onclick" not in webui.INDEX_HTML


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_js_is_syntactically_valid():
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(webui.APP_JS)
    try:
        result = subprocess.run(["node", "--check", f.name], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        os.unlink(f.name)


# --- snapshot content -------------------------------------------------------


def _config(tmp_path, **kwargs):
    return Config(model="m", working_dir=str(tmp_path), usage_file=str(tmp_path / "usage.json"), **kwargs)


def test_snapshot_without_a_workspace_still_reports_usage(tmp_path):
    llm._record_usage({"prompt_eval_count": 100, "eval_count": 50}, str(tmp_path / "usage.json"))
    llm.add_compute_seconds(90)
    snap = main._web_snapshot(_config(tmp_path, budget_tokens=1000, budget_minutes=5.0))
    json.dumps(snap)  # must be JSON-serializable
    assert snap["sessions"] == []
    assert snap["usage"]["session"] == {"prompt_tokens": 100, "completion_tokens": 50, "calls": 1}
    assert snap["usage"]["all_time"]["prompt_tokens"] == 100
    assert snap["usage"]["compute_seconds"] == 90
    assert snap["budget"] == {"tokens_limit": 1000, "minutes_limit": 5.0}
    expected = main._estimate_savings(snap["usage"]["all_time"], _config(tmp_path))
    assert snap["usage"]["estimated_saved_usd"] == expected > 0


def _workspace(tmp_path, histories):
    sessions = {}
    for name, history in histories.items():
        directory = tmp_path / name
        directory.mkdir(exist_ok=True)
        sessions[name] = main.REPLState(
            config=_config(directory), client=None, history=history, name=name
        )
    workspace = main.Workspace(sessions, next(iter(sessions)))
    for state in sessions.values():
        state.workspace = workspace
    main._web_workspace = workspace
    return workspace


def test_snapshot_describes_every_session(tmp_path):
    workspace = _workspace(tmp_path, {"one": [{"role": "user", "content": "hi"}], "two": []})
    workspace.sessions["two"].plan_mode = True
    snap = main._web_snapshot(_config(tmp_path))
    one, two = snap["sessions"]
    assert (one["name"], one["current"], two["current"], two["plan_mode"]) == ("one", True, False, True)
    assert one["directory"] == os.path.realpath(str(tmp_path / "one")) and one["model"] == "m"
    assert one["messages"] == [{"role": "user", "content": "hi"}] and one["message_count"] == 1
    json.dumps(snap)


def test_snapshot_message_conversion(tmp_path):
    history = [
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "read_file"}}, "weird", {"function": {}}]},
        {"role": "tool", "content": "file body", "name": "read_file"},
        {"role": "user", "content": 12345},
        {"role": "assistant", "content": None},
    ]
    _workspace(tmp_path, {"s": history})
    messages = main._web_snapshot(_config(tmp_path))["sessions"][0]["messages"]
    assert messages[0]["tool_calls"] == ["read_file", "?", "?"]
    assert messages[1] == {"role": "tool", "content": "file body", "name": "read_file"}
    assert messages[2]["content"] == "12345" and messages[3]["content"] == ""


def test_snapshot_truncates_long_content_and_limits_message_count(tmp_path):
    big = "x" * (main._WEB_MAX_CONTENT + 100)
    history = [{"role": "user", "content": f"m{i}"} for i in range(main._WEB_MAX_MESSAGES + 5)]
    history[-1] = {"role": "assistant", "content": big}
    _workspace(tmp_path, {"s": history})
    session_info = main._web_snapshot(_config(tmp_path))["sessions"][0]
    assert session_info["message_count"] == main._WEB_MAX_MESSAGES + 5
    assert len(session_info["messages"]) == main._WEB_MAX_MESSAGES
    assert session_info["messages"][0]["content"] == "m5"  # the latest ones are kept
    last = session_info["messages"][-1]["content"]
    assert last.endswith("[100 more characters]") and len(last) < len(big)


def test_snapshot_lists_recent_file_changes_per_session(tmp_path):
    from forge.tools import call_tool

    _workspace(tmp_path, {"a": [], "b": []})
    call_tool("write_file", {"path": "made.txt", "content": "x"}, str(tmp_path / "a"))
    a, b = main._web_snapshot(_config(tmp_path))["sessions"]
    assert a["changes"] == ["created made.txt"] and b["changes"] == []


# --- start-up and end-to-end ------------------------------------------------


def test_flags_parse():
    args, _ = main.parse_args(["--web", "--web-port", "0", "task"])
    assert args.web is True and args.web_port == 0
    assert main.parse_args([])[0].web is False and main.parse_args([])[0].web_port == webui.DEFAULT_PORT


def test_start_web_prints_the_url_and_serves(tmp_path, capsys):
    dashboard = main._start_web(_config(tmp_path), 0)
    try:
        assert dashboard is not None
        assert f"Dashboard: {dashboard.url}" in capsys.readouterr().err
        response, body = request(dashboard, "/api/state")
        assert response.status == 200 and "usage" in json.loads(body)
    finally:
        dashboard.stop()


def test_start_web_reports_a_busy_port_and_carries_on(tmp_path, capsys):
    first = main._start_web(_config(tmp_path), 0)
    try:
        capsys.readouterr()
        assert main._start_web(_config(tmp_path), first.port) is None
        assert "Dashboard not started" in capsys.readouterr().err
    finally:
        first.stop()


def test_the_running_repl_is_visible_through_the_dashboard(monkeypatch, tmp_path):
    seen = {}
    dashboard = main._start_web(_config(tmp_path), 0)

    def fake_run_task(task, history, client, model, **kwargs):
        new = list(history) + [{"role": "user", "content": task}, {"role": "assistant", "content": "reply!"}]
        return type("R", (), {"content": "reply!", "history": new})()

    inputs = iter(["do a thing"])

    def fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            # the REPL is still "running" here: read the live dashboard
            _, body = request(dashboard, "/api/state")
            seen["state"] = json.loads(body)
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(main.llm, "run_task", fake_run_task)
    try:
        main.run_repl(_config(tmp_path), client=None)
    finally:
        dashboard.stop()

    (only,) = seen["state"]["sessions"]
    assert [m["content"] for m in only["messages"]] == ["do a thing", "reply!"]
