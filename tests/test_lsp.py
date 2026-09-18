import io
import json
import os
import shlex
import sys
import time

import pytest

from forge import lsp
from forge.tools import call_tool

FAKE_SERVER = r'''
import json, os, sys

def read():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            break
        key, _, value = line.decode().partition(":")
        if key.lower() == "content-length":
            length = int(value)
    return json.loads(sys.stdin.buffer.read(length))

def send(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()

root = ""
while True:
    m = read()
    if m is None:
        break
    method = m.get("method")
    if method is None and m.get("id") == 900:
        open(os.environ["FAKE_LSP_OUT"], "w").write(json.dumps(m["result"]))
    elif method == "initialize":
        root = m["params"]["rootUri"]
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"capabilities": {}}})
    elif method == "initialized":
        send({"jsonrpc": "2.0", "id": 900, "method": "workspace/configuration",
              "params": {"items": [{"section": "a"}, {"section": "b"}]}})
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        doc = m["params"]["textDocument"]
        rng = {"start": {"line": 0, "character": 2}, "end": {"line": 0, "character": 3}}
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
              "params": {"uri": doc["uri"], "diagnostics": [
                  {"range": rng, "severity": 2, "message": "v%d" % doc["version"], "code": "X1"}]}})
    elif method == "textDocument/definition":
        loc = {"uri": root + "/a.py",
               "range": {"start": {"line": 0, "character": 4}, "end": {"line": 0, "character": 9}}}
        send({"jsonrpc": "2.0", "id": m["id"], "result": loc})
    elif method == "textDocument/references":
        rng = {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 1}}
        send({"jsonrpc": "2.0", "id": m["id"],
              "result": [{"uri": root + "/a.py", "range": rng}, {"uri": root + "/b.py", "range": rng}]})
    elif method == "textDocument/hover":
        send({"jsonrpc": "2.0", "id": m["id"],
              "result": {"contents": {"kind": "markdown", "value": "hover!"}}})
    elif method == "test/error":
        send({"jsonrpc": "2.0", "id": m["id"], "error": {"code": -1, "message": "boom"}})
    elif method == "test/die":
        sys.exit(0)
    elif method == "test/never":
        pass
    elif method == "shutdown":
        send({"jsonrpc": "2.0", "id": m["id"], "result": None})
    elif method == "exit":
        break
'''


@pytest.fixture
def fake_server(tmp_path, monkeypatch):
    script = tmp_path / "fake_lsp.py"
    script.write_text(FAKE_SERVER)
    out = tmp_path / "config_reply.json"
    monkeypatch.setenv("FAKE_LSP_OUT", str(out))
    monkeypatch.setenv("FORGE_LSP_COMMAND", shlex.join([sys.executable, str(script)]))
    yield out
    lsp.shutdown_all()


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("def greet(): pass\n")
    (root / "b.py").write_text("greet()\n")
    return root


# --- wire format & normalization --------------------------------------------


def test_frame_and_read_message_roundtrip():
    message = {"jsonrpc": "2.0", "id": 1, "method": "x", "params": {"ünï": "cødé"}}
    assert lsp._read_message(io.BytesIO(lsp._frame(message))) == message


def test_read_message_handles_eof_and_truncation():
    assert lsp._read_message(io.BytesIO(b"")) is None
    assert lsp._read_message(io.BytesIO(b"Content-Length: 50\r\n\r\n{}")) is None


def test_locations_normalizes_all_shapes():
    rng = {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 6}}
    assert lsp._locations(None) == []
    assert lsp._locations({"uri": "file:///a.py", "range": rng}) == [
        {"uri": "file:///a.py", "line": 3, "column": 5}
    ]
    link = {"targetUri": "file:///b.py", "targetSelectionRange": rng, "targetRange": rng}
    assert lsp._locations([link]) == [{"uri": "file:///b.py", "line": 3, "column": 5}]
    assert lsp._locations([{"uri": "file:///c.py"}]) == []


def test_hover_text_variants():
    assert lsp._hover_text(None) == ""
    assert lsp._hover_text("plain ") == "plain"
    assert lsp._hover_text({"kind": "markdown", "value": "md"}) == "md"
    assert lsp._hover_text(["a", {"value": "b"}]) == "a\n\nb"


def test_uri_to_path_decodes():
    assert lsp.uri_to_path("file:///tmp/my%20dir/a.py") == "/tmp/my dir/a.py"


# --- client against a fake server -------------------------------------------


def test_client_requests_and_normalization(fake_server, project):
    client = lsp.get_client(str(project))
    target = str(project / "b.py")
    (loc,) = client.definition(target, 1, 1)
    assert (loc["line"], loc["column"]) == (1, 5)
    assert lsp.uri_to_path(loc["uri"]).endswith("a.py")
    assert len(client.references(target, 1, 1)) == 2
    assert client.hover(target, 1, 1) == "hover!"


def test_client_sends_one_based_positions_as_zero_based(fake_server, project):
    client = lsp.get_client(str(project))
    seen = []
    original = client.request
    client.request = lambda method, params, **kw: (seen.append(params), original(method, params, **kw))[1]
    client.hover(str(project / "b.py"), 3, 7)
    assert seen[-1]["position"] == {"line": 2, "character": 6}


def test_client_answers_server_requests(fake_server, project):
    client = lsp.get_client(str(project))
    client.hover(str(project / "b.py"), 1, 1)
    deadline = time.time() + 3
    while not fake_server.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert json.loads(fake_server.read_text()) == [{}, {}]


def test_diagnostics_track_document_changes(fake_server, project):
    client = lsp.get_client(str(project))
    target = project / "b.py"
    assert client.diagnostics(str(target))[0]["message"] == "v1"
    assert client.diagnostics(str(target))[0]["message"] == "v1"  # unchanged: not resent
    target.write_text("greet(1)\n")
    assert client.diagnostics(str(target))[0]["message"] == "v2"


def test_error_response_raises(fake_server, project):
    client = lsp.get_client(str(project))
    with pytest.raises(lsp.LSPError, match="boom"):
        client.request("test/error", {})


def test_request_timeout_raises(fake_server, project):
    client = lsp.get_client(str(project))
    with pytest.raises(lsp.LSPError, match="timed out"):
        client.request("test/never", {}, timeout=0.3)


def test_server_dying_wakes_waiting_requests(fake_server, project):
    client = lsp.get_client(str(project))
    with pytest.raises(lsp.LSPError, match="exited"):
        client.request("test/die", {}, timeout=5)


def test_dead_server_is_restarted_on_next_use(fake_server, project):
    first = lsp.get_client(str(project))
    with pytest.raises(lsp.LSPError):
        first.request("test/die", {}, timeout=5)
    first._proc.wait(timeout=5)
    second = lsp.get_client(str(project))
    assert second is not first and second.alive


def test_client_is_reused_per_project(fake_server, project):
    assert lsp.get_client(str(project)) is lsp.get_client(str(project))


def test_missing_server_gives_install_hint(monkeypatch, project):
    monkeypatch.setenv("FORGE_LSP_COMMAND", "definitely-not-a-real-language-server")
    with pytest.raises(lsp.LSPError, match="not found"):
        lsp.get_client(str(project))


def test_server_that_cannot_start_raises(monkeypatch, project, tmp_path):
    script = tmp_path / "exits.py"
    script.write_text("import sys; sys.exit(1)")
    monkeypatch.setenv("FORGE_LSP_COMMAND", shlex.join([sys.executable, str(script)]))
    with pytest.raises(lsp.LSPError):
        lsp.get_client(str(project))
    assert lsp._clients == {}


# --- tools (through call_tool) ----------------------------------------------


def test_tool_find_definition_formats_location_with_source_line(fake_server, project):
    out = call_tool("find_definition", {"path": "b.py", "line": 1, "column": 1}, str(project))
    assert out == "a.py:1:5  def greet(): pass"


def test_tool_find_references_lists_all(fake_server, project):
    out = call_tool("find_references", {"path": "b.py", "line": 1, "column": 1}, str(project))
    assert out.splitlines()[0].startswith("a.py:1:1")
    assert out.splitlines()[1].startswith("b.py:1:1")


def test_tool_hover(fake_server, project):
    assert call_tool("hover", {"path": "b.py", "line": 1, "column": 1}, str(project)) == "hover!"


def test_tool_get_diagnostics_formats(fake_server, project):
    out = call_tool("get_diagnostics", {"path": "b.py"}, str(project))
    assert out == "b.py:1:3 warning: v1 [X1]"


def test_tool_accepts_numeric_strings(fake_server, project):
    out = call_tool("hover", {"path": "b.py", "line": "1", "column": "1"}, str(project))
    assert out == "hover!"


@pytest.mark.parametrize(
    "args, message",
    [
        ({"path": "../x.py", "line": 1, "column": 1}, "escapes"),
        ({"path": "nope.py", "line": 1, "column": 1}, "Not a file"),
        ({"path": "notes.txt", "line": 1, "column": 1}, "Python"),
        ({"path": "b.py", "line": 0, "column": 1}, "1-based"),
        ({"path": "b.py", "line": 1, "column": -3}, "1-based"),
        ({"path": "b.py", "line": "abc", "column": 1}, "positive integer"),
        ({"path": "b.py", "line": True, "column": 1}, "positive integer"),
    ],
)
def test_tool_validation_errors_never_need_a_server(project, monkeypatch, args, message):
    monkeypatch.setenv("FORGE_LSP_COMMAND", "definitely-not-a-real-language-server")
    (project / "notes.txt").write_text("x")
    out = call_tool("find_definition", args, str(project))
    assert out.startswith("Error:") and message in out


def test_tool_reports_missing_server_as_error(monkeypatch, project):
    monkeypatch.setenv("FORGE_LSP_COMMAND", "definitely-not-a-real-language-server")
    out = call_tool("get_diagnostics", {"path": "b.py"}, str(project))
    assert out.startswith("Error:") and "pip install pyright" in out


def test_lsp_tools_are_allowed_in_plan_mode(fake_server, project):
    out = call_tool("hover", {"path": "b.py", "line": 1, "column": 1}, str(project), read_only=True)
    assert out == "hover!"


# --- real pyright (skipped if it isn't installed) ---------------------------


def _pyright_available():
    os.environ.pop("FORGE_LSP_COMMAND", None)
    try:
        lsp.server_command()
        return True
    except lsp.LSPError:
        return False


needs_pyright = pytest.mark.skipif(not _pyright_available(), reason="pyright not installed")


@pytest.fixture(scope="module")
def pyright_project(tmp_path_factory):
    root = tmp_path_factory.mktemp("pyright_proj")
    (root / "a.py").write_text('def greet(name: str) -> str:\n    """Say hello."""\n    return "hi " + name\n')
    (root / "b.py").write_text('from a import greet\n\nx = greet("bob")\ny: int = greet("z")\nprint(undefined_name)\n')
    yield root
    lsp.shutdown_all()


@needs_pyright
def test_pyright_definition(pyright_project):
    out = call_tool("find_definition", {"path": "b.py", "line": 3, "column": 5}, str(pyright_project))
    assert out == "a.py:1:5  def greet(name: str) -> str:"


@needs_pyright
def test_pyright_references(pyright_project):
    out = call_tool("find_references", {"path": "a.py", "line": 1, "column": 5}, str(pyright_project))
    assert "b.py:3:5" in out and "b.py:4:10" in out and "a.py:1:5" in out


@needs_pyright
def test_pyright_hover(pyright_project):
    out = call_tool("hover", {"path": "b.py", "line": 3, "column": 5}, str(pyright_project))
    assert "def greet(name: str) -> str" in out and "Say hello." in out


@needs_pyright
def test_pyright_diagnostics(pyright_project):
    out = call_tool("get_diagnostics", {"path": "b.py"}, str(pyright_project))
    assert "b.py:4:10 error" in out and "undefined_name" in out
    clean = call_tool("get_diagnostics", {"path": "a.py"}, str(pyright_project))
    assert clean.startswith("No diagnostics")


@needs_pyright
def test_pyright_sees_edits_made_after_first_check(pyright_project):
    target = pyright_project / "c.py"
    target.write_text("x: int = 1\n")
    assert call_tool("get_diagnostics", {"path": "c.py"}, str(pyright_project)).startswith("No diagnostics")
    target.write_text('x: int = "no"\n')
    assert "error" in call_tool("get_diagnostics", {"path": "c.py"}, str(pyright_project))
