import os

import pytest

from forge import main, session, undo
from forge.config import Config
from forge.tools import call_tool


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(session, "SESSIONS_DIR", str(tmp_path / "saved_sessions"))
    undo.clear()
    yield
    undo.clear()


@pytest.fixture
def projects(tmp_path):
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    return a, b


def _state(directory):
    config = Config(model="m", working_dir=str(directory), session_file=session.default_path(str(directory)))
    state = main.REPLState(config=config, client=object())
    state.workspace = main.Workspace({state.name: state}, state.name)
    return state


def _cmd(line, state):
    return main.handle_slash_command(line, state)


# --- Config for an explicit directory ---------------------------------------


def test_config_from_env_reads_the_given_directorys_forge_toml(tmp_path, projects, monkeypatch):
    a, b = projects
    (a / "forge.toml").write_text('model = "alpha-model"\n')
    (b / "forge.toml").write_text('model = "beta-model"\n')
    monkeypatch.setenv("FORGE_WORKING_DIR", str(a))
    assert Config.from_env().model == "alpha-model"
    assert Config.from_env(str(b)).model == "beta-model"
    assert Config.from_env(str(b)).working_dir == str(b)


# --- /session ---------------------------------------------------------------


def test_list_with_one_session(projects):
    state = _state(projects[0])
    assert _cmd("/session", state) == f"* main: {os.path.realpath(projects[0])} (0 messages)"
    assert _cmd("/session list", state) == _cmd("/sessions", state)


def test_new_opens_and_switches_with_its_own_config_and_history(projects):
    a, b = projects
    (b / "forge.toml").write_text('model = "beta-model"\n')
    state = _state(a)
    out = _cmd(f"/session new {b}", state)
    assert "Opened session 'beta'" in out
    ws = state.workspace
    assert ws.current == "beta"
    opened = ws.state
    assert opened.config.model == "beta-model" and opened.history == []
    assert opened.config.working_dir == str(b)
    assert opened.client is state.client and opened.budget is state.budget  # shared


def test_new_accepts_a_name_and_relative_paths(projects):
    a, b = projects
    state = _state(a)
    _cmd("/session new ../beta other", state)
    assert list(state.workspace.sessions) == ["main", "other"]


def test_new_uses_the_current_sessions_host(projects):
    a, b = projects
    state = _state(a)
    state.config.host = "http://elsewhere:1"
    _cmd(f"/session new {b}", state)
    assert state.workspace.state.config.host == "http://elsewhere:1"


def test_new_resumes_a_saved_history_for_that_directory(projects):
    a, b = projects
    saved = [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}]
    session.save(session.default_path(str(b)), saved)
    state = _state(a)
    out = _cmd(f"/session new {b}", state)
    assert "resumed 2 messages" in out and state.workspace.state.history == saved


@pytest.mark.parametrize(
    "build, message",
    [
        (lambda a, b: f"/session new {a}/missing", "not a directory"),
        (lambda a, b: f"/session new {a}", "already open as session 'main'"),
        (lambda a, b: "/session new", "Usage:"),
        (lambda a, b: "/session frobnicate", "Usage:"),
        (lambda a, b: "/session switch nope", "no session named"),
        (lambda a, b: "/session close nope", "no session named"),
        (lambda a, b: "/session close main", "only session"),
        (lambda a, b: '/session new "unterminated', "could not parse"),
    ],
)
def test_error_cases_change_nothing(projects, build, message):
    a, b = projects
    state = _state(a)
    out = _cmd(build(a, b), state)
    assert message in out
    assert list(state.workspace.sessions) == ["main"] and state.workspace.current == "main"


def test_duplicate_explicit_name_is_rejected_but_derived_names_are_uniquified(projects, tmp_path):
    a, b = projects
    state = _state(a)
    assert "already exists" in _cmd(f"/session new {b} main", state)
    other = tmp_path / "x" / "alpha"
    other.mkdir(parents=True)
    _cmd(f"/session new {other}", state)  # derived name 'alpha' does not clash with 'main'
    third = tmp_path / "y" / "alpha"
    third.mkdir(parents=True)
    _cmd(f"/session new {third}", state)
    assert list(state.workspace.sessions) == ["main", "alpha", "alpha-2"]


def test_invalid_forge_toml_in_the_new_directory_is_reported(projects):
    a, b = projects
    (b / "forge.toml").write_text("bogus = 1\n")
    state = _state(a)
    assert _cmd(f"/session new {b}", state).startswith("Error:")
    assert list(state.workspace.sessions) == ["main"]


def test_switch_and_list_marker(projects):
    a, b = projects
    state = _state(a)
    _cmd(f"/session new {b}", state)
    _cmd("/session switch main", state)
    assert state.workspace.current == "main"
    listing = _cmd("/session list", state).splitlines()
    assert listing[0].startswith("* main:") and listing[1].startswith("  beta:")


def test_close_current_falls_back_and_keeps_saved_history(projects):
    a, b = projects
    state = _state(a)
    _cmd(f"/session new {b}", state)
    session.save(state.workspace.state.config.session_file, [{"role": "user", "content": "x"}])
    out = _cmd("/session close beta", state)
    assert "Now in 'main'" in out and state.workspace.current == "main"
    assert session.load(session.default_path(str(b))) is not None


def test_close_a_non_current_session(projects):
    a, b = projects
    state = _state(a)
    _cmd(f"/session new {b}", state)
    _cmd("/session switch main", state)
    assert "Now in" not in _cmd("/session close beta", state)
    assert list(state.workspace.sessions) == ["main"]


def test_state_without_a_workspace():
    state = main.REPLState(config=Config(), client=None)
    assert "aren't available" in _cmd("/session", state)


def test_help_mentions_session():
    assert "/session" in main.HELP_TEXT


# --- per-session state ------------------------------------------------------


def test_plan_mode_and_images_are_per_session(projects):
    a, b = projects
    state = _state(a)
    _cmd("/plan", state)
    _cmd(f"/session new {b}", state)
    opened = state.workspace.state
    assert opened.plan_mode is False and state.plan_mode is True
    assert "plan mode" in _cmd("/session list", opened)
    assert opened.pending_images == [] and opened.auto_model and opened.last_model is None


def test_prompt_shows_the_session_name_only_when_there_are_several(projects):
    a, b = projects
    state = _state(a)
    assert main._repl_prompt(state) == "> "
    _cmd("/plan", state)
    assert main._repl_prompt(state) == "[plan] > "
    _cmd(f"/session new {b}", state)
    assert main._repl_prompt(state.workspace.state) == "[beta] > "
    assert main._repl_prompt(state) == "[main plan] > "


# --- undo is scoped to the session's directory ------------------------------


def test_undo_only_reverts_changes_made_in_that_directory(projects):
    a, b = projects
    call_tool("write_file", {"path": "a.txt", "content": "A"}, str(a))
    call_tool("write_file", {"path": "b.txt", "content": "B"}, str(b))

    state_a = _state(a)
    assert "Removed a.txt" in _cmd("/undo", state_a)
    assert (b / "b.txt").exists()  # beta's change untouched
    assert _cmd("/undo", state_a) == "Nothing to undo."

    state_b = _state(b)
    assert "created b.txt" in _cmd("/undo list", state_b)
    assert "Removed b.txt" in _cmd("/undo", state_b)


def test_undo_skips_other_directories_entries_but_finds_older_own_ones(projects):
    a, b = projects
    call_tool("write_file", {"path": "first.txt", "content": "1"}, str(a))
    call_tool("write_file", {"path": "other.txt", "content": "x"}, str(b))
    assert undo.undo(str(a)) == ["Removed first.txt (forge created it)"]
    assert (b / "other.txt").exists() and undo.count() == 1


def test_identical_entries_are_removed_by_identity(tmp_path):
    p = str(tmp_path / "f.txt")
    (tmp_path / "f.txt").write_text("v")
    scope = os.path.realpath(str(tmp_path))
    undo.record(p, b"old", b"v", scope=scope)
    undo.record(p, b"old", b"v", scope=scope)
    undo.undo(str(tmp_path), force=True)
    assert undo.count() == 1


# --- the REPL loop ----------------------------------------------------------


def test_repl_routes_tasks_to_the_active_sessions_directory_and_history(monkeypatch, projects):
    a, b = projects
    calls = []

    def fake_run_task(task, history, client, model, base_dir=".", **kwargs):
        calls.append((task, base_dir, len(history)))
        new = list(history) + [{"role": "user", "content": task}, {"role": "assistant", "content": "ok"}]
        return type("R", (), {"content": "ok", "history": new})()

    inputs = iter(
        [
            "task one",  # main
            f"/session new {b}",
            "task two",  # beta, fresh history
            "task three",  # beta, second turn
            "/session switch main",
            "task four",  # main again, history from task one
        ]
    )

    def fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(main.llm, "run_task", fake_run_task)
    config = Config(model="m", working_dir=str(a), session_file=session.default_path(str(a)))
    main.run_repl(config, client=object())

    assert [(t, os.path.realpath(d) if d else d, n) for t, d, n in calls] == [
        ("task one", str(a), 0),
        ("task two", os.path.realpath(str(b)), 0),
        ("task three", os.path.realpath(str(b)), 2),
        ("task four", str(a), 2),
    ]
    # each directory persisted its own conversation
    assert len(session.load(session.default_path(str(a)))) == 4
    assert len(session.load(session.default_path(os.path.realpath(str(b))))) == 4
