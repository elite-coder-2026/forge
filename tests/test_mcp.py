import json
import os
import sys
import time

import pytest

from forge import main, mcp
from forge.config import Config, ConfigError, MCPServerConfig, load_project_file
from forge.tools import TOOL_SCHEMAS, call_tool, tool_schemas_for

FAKE_SERVER = r'''
import json, os, sys, time

def send(message):
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()

print("server starting (this line is not JSON-RPC)", flush=True)

SIMPLE = {"type": "object", "properties": {}}
TOOLS = [
    {"name": "echo", "description": "Echo text",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
     "annotations": {"readOnlyHint": True}},
    {"name": "boom", "description": "Always fails", "inputSchema": SIMPLE},
    {"name": "weird name!", "description": "needs sanitizing"},
    {"name": "image", "inputSchema": SIMPLE},
    {"name": "struct", "inputSchema": SIMPLE},
    {"name": "slow", "inputSchema": SIMPLE},
    {"name": "die", "inputSchema": SIMPLE},
    {"name": "fail", "inputSchema": SIMPLE},
    {"name": "env", "inputSchema": SIMPLE},
]
PAGE_TWO = [{"name": "later", "description": "on page two", "inputSchema": SIMPLE}]

def text(value, error=False):
    return {"content": [{"type": "text", "text": value}], "isError": error}

for line in sys.stdin:
    m = json.loads(line)
    method, mid = m.get("method"), m.get("id")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1"}}})
    elif method == "notifications/initialized":
        send({"jsonrpc": "2.0", "id": "p1", "method": "ping"})
        send({"jsonrpc": "2.0", "id": "s1", "method": "sampling/createMessage", "params": {}})
    elif method is None and mid in ("p1", "s1"):
        with open(os.environ["FAKE_MCP_OUT"], "a") as f:
            f.write(json.dumps(m) + "\n")
    elif method == "tools/list":
        if (m.get("params") or {}).get("cursor") == "page2":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": PAGE_TWO}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS, "nextCursor": "page2"}})
    elif method == "tools/call":
        name = m["params"]["name"]
        args = m["params"].get("arguments") or {}
        if name == "echo":
            result = text(args.get("text", ""))
        elif name == "boom":
            result = text("it broke", error=True)
        elif name == "later":
            result = text("later ok")
        elif name == "weird name!":
            result = text("sanitized ok")
        elif name == "image":
            result = {"content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"}]}
        elif name == "struct":
            result = {"content": [], "structuredContent": {"answer": 42}}
        elif name == "env":
            result = text(os.environ.get("MY_VAR", "<unset>"))
        elif name == "fail":
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32000, "message": "server said no"}})
            continue
        elif name == "die":
            sys.exit(0)
        elif name == "slow":
            time.sleep(30)
            continue
        send({"jsonrpc": "2.0", "id": mid, "result": result})
'''


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(mcp, "TRUST_FILE", str(tmp_path / "trust.json"))
    mcp.set_manager(None)
    yield
    mcp.set_manager(None)


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "fake_server.py"
    path.write_text(FAKE_SERVER)
    return str(path)


@pytest.fixture
def out_file(tmp_path):
    return tmp_path / "server_out.jsonl"


def _server(script, out_file, timeout=10.0, env=None):
    return MCPServerConfig(
        sys.executable, [script], {"FAKE_MCP_OUT": str(out_file), **(env or {})}, timeout
    )


@pytest.fixture
def manager(script, out_file, tmp_path):
    m = mcp.MCPManager()
    m.start({"fake": _server(script, out_file)}, str(tmp_path))
    yield m
    m.shutdown()


# --- discovery & schemas ----------------------------------------------------


def test_connects_and_collects_tools_across_pages(manager):
    assert manager.errors == {}
    names = set(manager.tools)
    assert {"mcp__fake__echo", "mcp__fake__later", "mcp__fake__boom"} <= names


def test_schemas_use_namespaced_names_and_input_schema(manager):
    schema = next(s for s in manager.schemas() if s["function"]["name"] == "mcp__fake__echo")
    assert schema["type"] == "function"
    assert schema["function"]["description"] == "[MCP server: fake] Echo text"
    assert schema["function"]["parameters"]["required"] == ["text"]


def test_missing_input_schema_gets_an_empty_object_schema(manager):
    schema = next(s for s in manager.schemas() if s["function"]["name"] == "mcp__fake__weird_name_")
    assert schema["function"]["parameters"] == {"type": "object", "properties": {}}


def test_tool_names_are_sanitized_and_still_routed_to_the_real_name(manager):
    assert manager.tools["mcp__fake__weird_name_"].name == "weird name!"
    assert manager.call("mcp__fake__weird_name_", {}) == "sanitized ok"


def test_plan_mode_offers_only_read_only_tools(manager):
    assert [s["function"]["name"] for s in manager.schemas(read_only=True)] == ["mcp__fake__echo"]
    assert manager.is_read_only("mcp__fake__echo") and not manager.is_read_only("mcp__fake__boom")


def test_describe_lists_servers_tools_and_read_only_marks(manager):
    text = manager.describe()
    assert "fake: 10 tool(s)" in text and "mcp__fake__echo [read-only]" in text


def test_qualified_name_sanitizes_truncates_and_dedupes():
    assert mcp._qualified_name("s", "a b/c", set()) == "mcp__s__a_b_c"
    long = mcp._qualified_name("s", "x" * 100, set())
    assert len(long) == 64
    taken = {long}
    second = mcp._qualified_name("s", "x" * 100, taken)
    assert second != long and len(second) <= 64
    assert mcp._qualified_name("s", "t", {"mcp__s__t"}) == "mcp__s__t_2"


# --- calling ----------------------------------------------------------------


def test_call_returns_text(manager):
    assert manager.call("mcp__fake__echo", {"text": "hello"}) == "hello"


def test_tool_reported_error_is_prefixed(manager):
    assert manager.call("mcp__fake__boom", {}) == "Error: it broke"


def test_non_text_results(manager):
    assert manager.call("mcp__fake__image", {}) == "[image content: image/png]"
    assert json.loads(manager.call("mcp__fake__struct", {})) == {"answer": 42}


def test_protocol_error_raises_mcp_error(manager):
    with pytest.raises(mcp.MCPError, match="server said no"):
        manager.call("mcp__fake__fail", {})


def test_unknown_tool_raises(manager):
    with pytest.raises(mcp.MCPError, match="unknown"):
        manager.call("mcp__fake__nope", {})


def test_slow_call_times_out(script, out_file, tmp_path):
    m = mcp.MCPManager()
    m.start({"fake": _server(script, out_file, timeout=10)}, str(tmp_path))
    m.clients["fake"].config.timeout = 0.4  # after startup, so only the call is short
    try:
        with pytest.raises(mcp.MCPError, match="timed out"):
            m.call("mcp__fake__slow", {})
    finally:
        m.shutdown()


def test_server_dying_mid_call_raises_and_later_calls_say_it_exited(manager):
    with pytest.raises(mcp.MCPError, match="exited"):
        manager.call("mcp__fake__die", {})
    manager.clients["fake"]._proc.wait(timeout=5)
    with pytest.raises(mcp.MCPError, match="has exited"):
        manager.call("mcp__fake__echo", {"text": "x"})


def test_client_answers_server_requests(manager, out_file):
    deadline = time.time() + 5
    while time.time() < deadline and (not out_file.exists() or len(out_file.read_text().splitlines()) < 2):
        time.sleep(0.05)
    replies = {r["id"]: r for r in map(json.loads, out_file.read_text().splitlines())}
    assert replies["p1"]["result"] == {}  # ping answered
    assert replies["s1"]["error"]["code"] == -32601  # unsupported request refused


def test_env_values_are_expanded_from_the_environment(script, out_file, tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_FROM_SHELL", "s3cret")
    m = mcp.MCPManager()
    m.start({"fake": _server(script, out_file, env={"MY_VAR": "${SECRET_FROM_SHELL}"})}, str(tmp_path))
    try:
        assert m.call("mcp__fake__env", {}) == "s3cret"
    finally:
        m.shutdown()


def test_shutdown_stops_the_process(manager):
    proc = manager.clients["fake"]._proc
    manager.shutdown()
    assert proc.wait(timeout=5) is not None
    assert manager.tools == {} and manager.clients == {}


# --- startup failures -------------------------------------------------------


def test_one_bad_server_does_not_stop_the_others(script, out_file, tmp_path):
    m = mcp.MCPManager()
    m.start(
        {
            "missing": MCPServerConfig("definitely-not-a-real-command-xyz", [], {}, 5),
            "fake": _server(script, out_file),
        },
        str(tmp_path),
    )
    try:
        assert "missing" in m.errors and "could not start" in m.errors["missing"]
        assert "mcp__fake__echo" in m.tools
        assert "failed to start" in m.describe()
    finally:
        m.shutdown()


def test_server_that_exits_immediately_is_reported(tmp_path):
    quitter = tmp_path / "quit.py"
    quitter.write_text("import sys; sys.exit(3)")
    m = mcp.MCPManager()
    m.start({"quit": MCPServerConfig(sys.executable, [str(quitter)], {}, 5)}, str(tmp_path))
    assert "quit" in m.errors and m.tools == {}


# --- result formatting ------------------------------------------------------


@pytest.mark.parametrize(
    "result, expected",
    [
        ({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}, "a\nb"),
        ({"content": [{"type": "resource", "resource": {"uri": "u", "text": "body"}}]}, "body"),
        ({"content": [{"type": "resource", "resource": {"uri": "file:///x"}}]}, "[resource: file:///x]"),
        ({"content": [{"type": "resource_link", "uri": "file:///y"}]}, "[resource link: file:///y]"),
        ({"content": [{"type": "audio", "mimeType": "audio/wav"}]}, "[audio content: audio/wav]"),
        ({"content": []}, "(no output)"),
        ({}, "(no output)"),
        ({"content": [{"type": "text", "text": "bad"}], "isError": True}, "Error: bad"),
        ({"content": ["not a dict"]}, "(no output)"),
    ],
)
def test_format_result(result, expected):
    assert mcp.format_result(result) == expected


def test_format_result_truncates_huge_output():
    out = mcp.format_result({"content": [{"type": "text", "text": "x" * (mcp.MAX_RESULT_CHARS + 500)}]})
    assert len(out) < mcp.MAX_RESULT_CHARS + 100 and "500 more characters" in out


# --- trust ------------------------------------------------------------------


def _defs(command="tool", args=None):
    return {"s": MCPServerConfig(command, args or [], {}, 60)}


def test_fingerprint_is_stable_and_changes_with_any_definition_change():
    base = mcp.fingerprint(_defs())
    assert base == mcp.fingerprint(_defs())
    assert base != mcp.fingerprint(_defs(command="other"))
    assert base != mcp.fingerprint(_defs(args=["--x"]))
    changed_env = _defs()
    changed_env["s"].env = {"K": "v"}
    assert base != mcp.fingerprint(changed_env)


def test_trust_roundtrip_per_project_and_invalidated_by_edits(tmp_path):
    project, other = str(tmp_path / "p"), str(tmp_path / "q")
    assert not mcp.is_trusted(project, _defs())
    mcp.trust(project, _defs())
    assert mcp.is_trusted(project, _defs())
    assert not mcp.is_trusted(other, _defs())  # approval is per project
    assert not mcp.is_trusted(project, _defs(command="different"))  # and per definition


def test_corrupt_trust_file_means_untrusted(tmp_path):
    open(mcp.TRUST_FILE, "w").write("not json")
    assert not mcp.is_trusted(str(tmp_path), _defs())


# --- tool layer integration -------------------------------------------------


def test_without_a_manager_builtin_schemas_are_unchanged():
    assert tool_schemas_for(read_only=False) == TOOL_SCHEMAS


def test_schemas_include_mcp_tools_and_plan_mode_filters_them(manager):
    mcp.set_manager(manager)
    full = {s["function"]["name"] for s in tool_schemas_for(read_only=False)}
    assert "mcp__fake__boom" in full and "run_shell" in full
    plan = {s["function"]["name"] for s in tool_schemas_for(read_only=True)}
    assert "mcp__fake__echo" in plan and "mcp__fake__boom" not in plan


def test_call_tool_routes_to_mcp(manager, tmp_path):
    mcp.set_manager(manager)
    assert call_tool("mcp__fake__echo", {"text": "hi"}, str(tmp_path)) == "hi"


def test_call_tool_plan_mode_refuses_non_read_only_mcp_tool(manager, tmp_path):
    mcp.set_manager(manager)
    out = call_tool("mcp__fake__boom", {}, str(tmp_path), read_only=True)
    assert out.startswith("Error:") and "plan mode" in out
    assert call_tool("mcp__fake__echo", {"text": "ok"}, str(tmp_path), read_only=True) == "ok"


def test_call_tool_turns_mcp_failures_into_error_strings(manager, tmp_path):
    mcp.set_manager(manager)
    assert call_tool("mcp__fake__fail", {}, str(tmp_path)).startswith("Error: ")
    assert "server said no" in call_tool("mcp__fake__fail", {}, str(tmp_path))


def test_mcp_tool_name_unknown_without_manager(tmp_path):
    assert call_tool("mcp__fake__echo", {}, str(tmp_path)) == "Error: unknown tool 'mcp__fake__echo'"


def test_set_manager_shuts_down_the_previous_one(script, out_file, tmp_path):
    first = mcp.MCPManager()
    first.start({"fake": _server(script, out_file)}, str(tmp_path))
    proc = first.clients["fake"]._proc
    mcp.set_manager(first)
    mcp.set_manager(None)
    assert proc.wait(timeout=5) is not None


# --- config parsing ---------------------------------------------------------


def _toml(tmp_path, text):
    (tmp_path / "forge.toml").write_text(text)
    return str(tmp_path)


def test_config_parses_servers(tmp_path, monkeypatch):
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_WORKING_DIR", _toml(
        tmp_path,
        '[mcp_servers.fs]\ncommand = "npx"\nargs = ["-y", "server-filesystem", "."]\ntimeout = 30\n'
        '[mcp_servers.gh]\ncommand = "gh-mcp"\n[mcp_servers.gh.env]\nTOKEN = "${GITHUB_TOKEN}"\n',
    ))
    servers = Config.from_env().mcp_servers
    assert servers["fs"] == MCPServerConfig("npx", ["-y", "server-filesystem", "."], {}, 30.0)
    assert servers["gh"].env == {"TOKEN": "${GITHUB_TOKEN}"}  # expanded at launch, not parse time
    assert servers["gh"].timeout == 60.0


def test_config_default_has_no_servers():
    assert Config().mcp_servers == {}


@pytest.mark.parametrize(
    "text, message",
    [
        ("mcp_servers = 5\n", "must be dict"),
        ("[mcp_servers]\nfs = 5\n", "must be a table"),
        ("[mcp_servers.fs]\nargs = []\n", "'command' is required"),
        ('[mcp_servers.fs]\ncommand = ""\n', "'command' is required"),
        ('[mcp_servers.fs]\ncommand = "x"\nargs = "nope"\n', "'args' must be a list"),
        ('[mcp_servers.fs]\ncommand = "x"\nargs = [1]\n', "'args' must be a list"),
        ('[mcp_servers.fs]\ncommand = "x"\nenv = {A = 1}\n', "'env' must be a table"),
        ('[mcp_servers.fs]\ncommand = "x"\ntimeout = 0\n', "'timeout'"),
        ('[mcp_servers.fs]\ncommand = "x"\ntimeout = true\n', "'timeout'"),
        ('[mcp_servers.fs]\ncommand = "x"\nbogus = 1\n', "unknown key"),
        ('[mcp_servers."bad name!"]\ncommand = "x"\n', "server names"),
    ],
)
def test_config_rejects_invalid_server_tables(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load_project_file(_toml(tmp_path, text))


# --- main: startup, trust, /mcp ---------------------------------------------


def _config(tmp_path, script, out_file):
    return Config(working_dir=str(tmp_path), mcp_servers={"fake": _server(script, out_file)})


def test_start_mcp_is_a_noop_without_servers(tmp_path, capsys):
    main._start_mcp(Config(working_dir=str(tmp_path)), trust_flag=False)
    assert mcp.get_manager() is None and capsys.readouterr().err == ""


def test_untrusted_and_non_interactive_does_not_start_anything(tmp_path, script, out_file, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    main._start_mcp(_config(tmp_path, script, out_file), trust_flag=False)
    assert mcp.get_manager() is None
    err = capsys.readouterr().err
    assert "not started" in err and "--trust-mcp" in err


def test_trust_flag_approves_starts_and_remembers(tmp_path, script, out_file, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    config = _config(tmp_path, script, out_file)
    main._start_mcp(config, trust_flag=True)
    assert mcp.get_manager() is not None
    assert "MCP: connected fake (10 tools)" in capsys.readouterr().err
    assert mcp.is_trusted(config.working_dir, config.mcp_servers)

    mcp.set_manager(None)  # next run needs no flag
    main._start_mcp(config, trust_flag=False)
    assert mcp.get_manager() is not None


def test_interactive_approval_yes_starts_and_no_does_not(tmp_path, script, out_file, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: True)
    config = _config(tmp_path, script, out_file)

    monkeypatch.setattr("builtins.input", lambda *_: "n")
    main._start_mcp(config, trust_flag=False)
    assert mcp.get_manager() is None
    assert "MCP servers not started" in capsys.readouterr().err
    assert not mcp.is_trusted(config.working_dir, config.mcp_servers)

    monkeypatch.setattr("builtins.input", lambda *_: "y")
    main._start_mcp(config, trust_flag=False)
    assert mcp.get_manager() is not None
    assert mcp.is_trusted(config.working_dir, config.mcp_servers)


def test_interactive_prompt_shows_the_commands_that_would_run(tmp_path, script, out_file, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    main._start_mcp(_config(tmp_path, script, out_file), trust_flag=False)
    err = capsys.readouterr().err
    assert "fake:" in err and script in err


def test_eof_at_the_approval_prompt_means_no(tmp_path, script, out_file, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: True)

    def eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    main._start_mcp(_config(tmp_path, script, out_file), trust_flag=False)
    assert mcp.get_manager() is None


def test_changed_definitions_require_approval_again(tmp_path, script, out_file, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    config = _config(tmp_path, script, out_file)
    main._start_mcp(config, trust_flag=True)
    mcp.set_manager(None)

    config.mcp_servers["fake"].args = [script, "--changed"]
    main._start_mcp(config, trust_flag=False)
    assert mcp.get_manager() is None


def test_failed_server_is_reported_but_startup_continues(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    config = Config(
        working_dir=str(tmp_path),
        mcp_servers={"bad": MCPServerConfig("definitely-not-a-real-command-xyz", [], {}, 5)},
    )
    main._start_mcp(config, trust_flag=True)
    assert "server 'bad' failed to start" in capsys.readouterr().err


def test_mcp_command(manager, tmp_path):
    state = main.REPLState(config=Config(), client=None)
    assert "No MCP servers connected" in main.handle_slash_command("/mcp", state)
    mcp.set_manager(manager)
    assert "mcp__fake__echo" in main.handle_slash_command("/mcp", state)


def test_trust_flag_and_help_text():
    args, _ = main.parse_args(["--trust-mcp", "task"])
    assert args.trust_mcp is True
    assert "/mcp" in main.HELP_TEXT


# --- real server from the official SDK (skipped if it isn't installed) ------


def _sdk_available():
    try:
        import mcp.server.mcpserver  # noqa: F401

        return True
    except ImportError:
        return False


SDK_SERVER = '''
from mcp.server.mcpserver import MCPServer

server = MCPServer("demo")

@server.tool()
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

if __name__ == "__main__":
    server.run()
'''


@pytest.mark.skipif(not _sdk_available(), reason="mcp SDK not installed")
def test_real_sdk_server(tmp_path):
    (tmp_path / "server.py").write_text(SDK_SERVER)
    m = mcp.MCPManager()
    m.start(
        {"demo": MCPServerConfig(sys.executable, [str(tmp_path / "server.py")], {}, 30)},
        str(tmp_path),
    )
    try:
        assert m.errors == {}
        schema = m.schemas()[0]["function"]
        assert schema["name"] == "mcp__demo__add" and "Add two numbers" in schema["description"]
        assert m.call("mcp__demo__add", {"a": 2, "b": 3}) == "5"
        assert m.call("mcp__demo__add", {"a": "x"}).startswith("Error:")  # validation error
    finally:
        m.shutdown()
