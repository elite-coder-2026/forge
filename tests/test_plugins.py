import os
import pathlib
import textwrap

import pytest

from forge import llm, main, plugins
from forge.config import Config
from forge.tools import TOOL_SCHEMAS, call_tool, tool_schemas_for

RESERVED = {"read_file", "write_file", "run_shell"}

HELLO = '''
from forge.plugins import tool

@tool(
    description="Say hello.",
    parameters={"name": {"type": "string", "description": "Who to greet."}},
    read_only=True,
)
def hello(base_dir, name):
    return f"hello {name} from {base_dir}"
'''


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(plugins, "USER_PLUGIN_DIR", str(tmp_path / "user_plugins"))
    monkeypatch.setattr(plugins, "TRUST_FILE", str(tmp_path / "plugin_trust.json"))
    plugins.set_registry(None)
    yield
    plugins.set_registry(None)


@pytest.fixture
def plugin_dir(tmp_path):
    path = tmp_path / "plugins"
    path.mkdir()
    return path


def _write(directory, name, source):
    (directory / name).write_text(textwrap.dedent(source))


def _load(directory, reserved=RESERVED):
    registry = plugins.PluginRegistry()
    registry.load(str(directory), set(reserved))
    return registry


# --- declaring tools --------------------------------------------------------


def test_loads_a_tool_with_schema(plugin_dir):
    _write(plugin_dir, "hello.py", HELLO)
    registry = _load(plugin_dir)
    assert registry.errors == {}
    (schema,) = registry.schemas()
    assert schema == {
        "type": "function",
        "function": {
            "name": "hello",
            "description": "Say hello.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "description": "Who to greet."}},
                "required": ["name"],
            },
        },
    }
    assert registry.is_read_only("hello")


def test_required_defaults_to_all_and_can_be_narrowed(plugin_dir):
    _write(
        plugin_dir,
        "t.py",
        '''
        from forge.plugins import tool

        @tool(description="d", parameters={"a": {"type": "string"}, "b": {"type": "string"}}, required=["a"])
        def both(base_dir, a, b="x"):
            return a + b

        @tool(description="d", parameters={"a": {"type": "string"}})
        def all_required(base_dir, a):
            return a

        @tool(description="no params")
        def bare(base_dir):
            return "ok"
        ''',
    )
    tools = _load(plugin_dir).tools
    assert tools["both"].schema["required"] == ["a"]
    assert tools["all_required"].schema["required"] == ["a"]
    assert tools["bare"].schema == {"type": "object", "properties": {}, "required": []}


def test_name_override_multiple_tools_and_not_read_only_by_default(plugin_dir):
    _write(
        plugin_dir,
        "t.py",
        '''
        from forge.plugins import tool

        @tool(description="one", name="custom_name")
        def one(base_dir): return "1"

        @tool(description="two")
        def two(base_dir): return "2"
        ''',
    )
    registry = _load(plugin_dir)
    assert set(registry.tools) == {"custom_name", "two"}
    assert not registry.is_read_only("two") and not registry.is_read_only("missing")


def test_only_public_python_files_are_loaded(plugin_dir):
    _write(plugin_dir, "hello.py", HELLO)
    _write(plugin_dir, "_private.py", HELLO.replace("hello", "private"))
    _write(plugin_dir, "notes.txt", "not python")
    (plugin_dir / "sub.py").mkdir()
    assert set(_load(plugin_dir).tools) == {"hello"}


def test_missing_directory_is_fine(tmp_path):
    assert _load(tmp_path / "nope").tools == {}


@pytest.mark.parametrize(
    "decorator, message",
    [
        ('@tool(description="d", name="bad name!")', "invalid tool name"),
        ('@tool(description="  ")', "needs a description"),
        ('@tool(description="d", parameters={"a": "not a dict"})', "map names to schema dicts"),
        ('@tool(description="d", parameters={"a": {}}, required=["zzz"])', "not in parameters"),
    ],
)
def test_invalid_declarations_are_reported_not_raised(plugin_dir, decorator, message):
    _write(plugin_dir, "t.py", f"from forge.plugins import tool\n\n{decorator}\ndef f(base_dir): return ''\n")
    registry = _load(plugin_dir)
    assert registry.tools == {}
    assert message in next(iter(registry.errors.values()))


def test_broken_plugin_does_not_stop_the_others(plugin_dir):
    _write(plugin_dir, "a_broken.py", "raise RuntimeError('boom at import')\n")
    _write(plugin_dir, "b_syntax.py", "def (:\n")
    _write(plugin_dir, "c_hello.py", HELLO)
    registry = _load(plugin_dir)
    assert "hello" in registry.tools
    errors = {os.path.basename(p): e for p, e in registry.errors.items()}
    assert "boom at import" in errors["a_broken.py"] and "SyntaxError" in errors["b_syntax.py"]


def test_reserved_and_duplicate_names_are_rejected(plugin_dir):
    _write(plugin_dir, "a.py", HELLO.replace("def hello", "def read_file"))
    _write(plugin_dir, "b.py", HELLO)
    _write(plugin_dir, "c.py", HELLO)  # same name as b
    _write(plugin_dir, "d.py", HELLO.replace("def hello", "def mcp__x__y"))
    registry = _load(plugin_dir)
    assert set(registry.tools) == {"hello"}
    assert registry.tools["hello"].source.endswith("b.py")
    errors = {os.path.basename(p): e for p, e in registry.errors.items()}
    assert "reserved" in errors["a.py"] and "already defined" in errors["c.py"] and "reserved" in errors["d.py"]


# --- calling ----------------------------------------------------------------


def test_call_passes_base_dir_and_arguments(plugin_dir):
    _write(plugin_dir, "hello.py", HELLO)
    assert _load(plugin_dir).call("hello", "/proj", {"name": "bob"}) == "hello bob from /proj"


def _registry_with(plugin_dir, body):
    _write(plugin_dir, "t.py", "from forge.plugins import tool\n\n" + textwrap.dedent(body))
    return _load(plugin_dir)


def test_non_string_results_are_stringified_and_empty_gets_a_placeholder(plugin_dir):
    registry = _registry_with(
        plugin_dir,
        '''
        @tool(description="d")
        def number(base_dir): return 42

        @tool(description="d")
        def empty(base_dir): return ""
        ''',
    )
    assert registry.call("number", ".", {}) == "42"
    assert registry.call("empty", ".", {}) == "(no output)"


def test_exceptions_become_error_strings(plugin_dir):
    registry = _registry_with(
        plugin_dir,
        '''
        @tool(description="d")
        def explode(base_dir): raise ValueError("nope")
        ''',
    )
    assert registry.call("explode", ".", {}) == "Error: ValueError: nope"


def test_wrong_arguments_become_a_clear_error(plugin_dir):
    _write(plugin_dir, "hello.py", HELLO)
    out = _load(plugin_dir).call("hello", ".", {"wrong": 1})
    assert out.startswith("Error: invalid arguments for 'hello'")


def test_huge_results_are_truncated(plugin_dir):
    registry = _registry_with(
        plugin_dir,
        f'''
        @tool(description="d")
        def big(base_dir): return "x" * {plugins.MAX_RESULT_CHARS + 500}
        ''',
    )
    out = registry.call("big", ".", {})
    assert "500 more characters" in out and len(out) < plugins.MAX_RESULT_CHARS + 100


def test_schemas_filter_for_plan_mode_and_describe(plugin_dir):
    registry = _registry_with(
        plugin_dir,
        '''
        @tool(description="reads", read_only=True)
        def reader(base_dir): return "r"

        @tool(description="writes")
        def writer(base_dir): return "w"
        ''',
    )
    assert {s["function"]["name"] for s in registry.schemas()} == {"reader", "writer"}
    assert [s["function"]["name"] for s in registry.schemas(read_only=True)] == ["reader"]
    text = registry.describe()
    assert "reader [read-only] - t.py: reads" in text and "writer - t.py: writes" in text


def test_describe_when_empty():
    assert plugins.PluginRegistry().describe() == "No plugins loaded."


# --- trust ------------------------------------------------------------------


def test_fingerprint_tracks_names_and_contents(plugin_dir):
    _write(plugin_dir, "a.py", HELLO)
    base = plugins.fingerprint(str(plugin_dir))
    assert base == plugins.fingerprint(str(plugin_dir))
    _write(plugin_dir, "a.py", HELLO + "\n# edited\n")
    edited = plugins.fingerprint(str(plugin_dir))
    assert edited != base
    _write(plugin_dir, "b.py", HELLO)
    assert plugins.fingerprint(str(plugin_dir)) != edited


def test_trust_is_per_project_and_invalidated_by_edits(tmp_path, plugin_dir):
    _write(plugin_dir, "a.py", HELLO)
    project, other = str(tmp_path / "p"), str(tmp_path / "q")
    assert not plugins.is_trusted(project, str(plugin_dir))
    plugins.trust(project, str(plugin_dir))
    assert plugins.is_trusted(project, str(plugin_dir))
    assert not plugins.is_trusted(other, str(plugin_dir))
    _write(plugin_dir, "a.py", HELLO + "\n# sneaky change\n")
    assert not plugins.is_trusted(project, str(plugin_dir))


def test_corrupt_trust_file_means_untrusted(plugin_dir, tmp_path):
    _write(plugin_dir, "a.py", HELLO)
    open(plugins.TRUST_FILE, "w").write("garbage")
    assert not plugins.is_trusted(str(tmp_path), str(plugin_dir))


# --- tool layer -------------------------------------------------------------


def _install(plugin_dir, body=HELLO):
    _write(plugin_dir, "hello.py", body)
    registry = _load(plugin_dir)
    plugins.set_registry(registry)
    return registry


def test_builtin_schemas_unchanged_without_or_with_empty_registry():
    assert tool_schemas_for(read_only=False) == TOOL_SCHEMAS
    plugins.set_registry(plugins.PluginRegistry())
    assert tool_schemas_for(read_only=False) == TOOL_SCHEMAS


def test_plugin_tools_join_the_offered_tools_and_plan_mode_filters(plugin_dir):
    _install(plugin_dir, HELLO + '\n@tool(description="w")\ndef writer(base_dir): return "w"\n')
    full = {s["function"]["name"] for s in tool_schemas_for(read_only=False)}
    plan = {s["function"]["name"] for s in tool_schemas_for(read_only=True)}
    assert {"hello", "writer", "run_shell"} <= full
    assert "hello" in plan and "writer" not in plan


def test_call_tool_dispatches_to_the_plugin_with_the_project_dir(plugin_dir, tmp_path):
    _install(plugin_dir)
    assert call_tool("hello", {"name": "bob"}, str(tmp_path)) == f"hello bob from {tmp_path}"


def test_call_tool_refuses_non_read_only_plugin_in_plan_mode(plugin_dir, tmp_path):
    _install(plugin_dir, HELLO.replace("read_only=True", "read_only=False"))
    out = call_tool("hello", {"name": "x"}, str(tmp_path), read_only=True)
    assert out.startswith("Error:") and "plan mode" in out
    _install(plugin_dir)  # read-only version is allowed
    assert call_tool("hello", {"name": "x"}, str(tmp_path), read_only=True).startswith("hello x")


def test_unknown_tool_without_registry(tmp_path):
    assert call_tool("hello", {}, str(tmp_path)) == "Error: unknown tool 'hello'"


def test_model_can_call_a_plugin_through_the_agent_loop(plugin_dir, tmp_path):
    _install(plugin_dir)

    class Client:
        def __init__(self):
            self.step = 0
            self.seen_tools = None

        def chat(self, **kwargs):
            self.step += 1
            self.seen_tools = {t["function"]["name"] for t in kwargs["tools"]}
            if self.step == 1:
                call = {"function": {"name": "hello", "arguments": {"name": "forge"}}}
                return {"message": {"content": "", "tool_calls": [call]}}
            return {"message": {"content": "done"}}

    client = Client()
    result = llm.run_task("greet", llm.new_history(), client, "m", base_dir=str(tmp_path))
    assert "hello" in client.seen_tools
    tool_message = next(m for m in result.history if m["role"] == "tool")
    assert tool_message["content"] == f"hello forge from {tmp_path}"


# --- startup loading --------------------------------------------------------


def _config(tmp_path):
    project = tmp_path / "project"
    (project / ".forge" / "plugins").mkdir(parents=True, exist_ok=True)
    return Config(working_dir=str(project)), project / ".forge" / "plugins"


def test_no_plugins_anywhere_is_silent_and_sets_nothing(tmp_path, capsys):
    config = Config(working_dir=str(tmp_path))
    main._load_plugins(config, trust_flag=False)
    assert plugins.get_registry() is None and capsys.readouterr().err == ""


def test_user_plugins_load_without_any_prompt(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    os.makedirs(plugins.USER_PLUGIN_DIR)
    _write(pathlib.Path(plugins.USER_PLUGIN_DIR), "u.py", HELLO)
    main._load_plugins(Config(working_dir=str(tmp_path)), trust_flag=False)
    assert plugins.get_registry().has_tool("hello")
    assert "Plugins: loaded hello" in capsys.readouterr().err


def test_untrusted_project_plugins_are_not_loaded_when_non_interactive(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    config, pdir = _config(tmp_path)
    _write(pdir, "p.py", HELLO)
    main._load_plugins(config, trust_flag=False)
    assert plugins.get_registry() is None or not plugins.get_registry().has_tool("hello")
    err = capsys.readouterr().err
    assert "not loaded" in err and "--trust-plugins" in err


def test_untrusted_project_does_not_block_user_plugins(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    os.makedirs(plugins.USER_PLUGIN_DIR)
    _write(pathlib.Path(plugins.USER_PLUGIN_DIR), "u.py", HELLO.replace("hello", "mine"))
    config, pdir = _config(tmp_path)
    _write(pdir, "p.py", HELLO)
    main._load_plugins(config, trust_flag=False)
    registry = plugins.get_registry()
    assert registry.has_tool("mine") and not registry.has_tool("hello")


def test_trust_flag_loads_and_remembers(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    config, pdir = _config(tmp_path)
    _write(pdir, "p.py", HELLO)
    main._load_plugins(config, trust_flag=True)
    assert plugins.get_registry().has_tool("hello")
    plugins.set_registry(None)
    main._load_plugins(config, trust_flag=False)  # remembered
    assert plugins.get_registry().has_tool("hello")


def test_interactive_prompt_lists_files_and_honors_the_answer(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: True)
    config, pdir = _config(tmp_path)
    _write(pdir, "p.py", HELLO)

    monkeypatch.setattr("builtins.input", lambda *_: "n")
    main._load_plugins(config, trust_flag=False)
    err = capsys.readouterr().err
    assert os.path.join(".forge", "plugins", "p.py") in err and "not loaded" in err
    assert not plugins.get_registry().has_tool("hello")

    monkeypatch.setattr("builtins.input", lambda *_: "y")
    main._load_plugins(config, trust_flag=False)
    assert plugins.get_registry().has_tool("hello")


def test_eof_at_the_prompt_means_no(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: True)
    config, pdir = _config(tmp_path)
    _write(pdir, "p.py", HELLO)

    def eof(*_):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    main._load_plugins(config, trust_flag=False)
    assert not plugins.get_registry().has_tool("hello")


def test_edited_project_plugin_needs_approval_again(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    config, pdir = _config(tmp_path)
    _write(pdir, "p.py", HELLO)
    main._load_plugins(config, trust_flag=True)
    _write(pdir, "p.py", HELLO + "\n# changed after approval\n")
    main._load_plugins(config, trust_flag=False)
    assert not plugins.get_registry().has_tool("hello")


def test_load_failures_are_reported_at_startup(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    config, pdir = _config(tmp_path)
    _write(pdir, "bad.py", "raise RuntimeError('kaboom')\n")
    main._load_plugins(config, trust_flag=True)
    assert "bad.py failed to load: RuntimeError: kaboom" in capsys.readouterr().err


def test_plugins_command_and_flags(plugin_dir):
    state = main.REPLState(config=Config(), client=None)
    assert "No plugins loaded" in main.handle_slash_command("/plugins", state)
    _install(plugin_dir)
    assert "hello [read-only]" in main.handle_slash_command("/plugins", state)
    args, _ = main.parse_args(["--trust-plugins", "task"])
    assert args.trust_plugins is True and "/plugins" in main.HELP_TEXT
