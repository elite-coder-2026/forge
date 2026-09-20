import os

import pytest

from forge import llm, main, modes


def _approver(edit_mode, answers, always=None):
    asked = []

    def ask(name, arguments):
        asked.append(name)
        return answers.pop(0)

    return modes.Approver(edit_mode, ask, always), asked


def test_default_mode_asks_for_edits_and_shell():
    approver, asked = _approver("default", [modes.ONCE, modes.DENY])
    assert approver("edit_file", {}) is True
    assert approver("run_shell", {}) is False
    assert asked == ["edit_file", "run_shell"]


def test_read_tools_never_ask():
    approver, asked = _approver("default", [])
    assert approver("read_file", {}) is True
    assert asked == []


def test_auto_mode_allows_edits_but_asks_for_shell():
    approver, asked = _approver("auto", [modes.DENY])
    assert approver("write_file", {}) is True
    assert approver("run_shell", {}) is False
    assert asked == ["run_shell"]


def test_dangerous_mode_never_asks():
    approver, asked = _approver("dangerous", [])
    assert approver("run_shell", {}) is True
    assert approver("edit_file", {}) is True
    assert asked == []


def test_always_remembers_the_category_for_the_session():
    always = set()
    approver, asked = _approver("default", [modes.ALWAYS, modes.DENY], always)
    assert approver("edit_file", {}) is True
    assert approver("write_file", {}) is True  # same category, not asked again
    assert asked == ["edit_file"]
    assert always == {"edits"}
    assert approver("run_shell", {}) is False  # shell is a separate category
    assert asked == ["edit_file", "run_shell"]


def test_describe_edit_shows_a_diff():
    title, target, body, is_diff = modes.describe(
        "edit_file", {"path": "a.py", "old_str": "x = 1", "new_str": "x = 2"}
    )
    assert (title, target, is_diff) == ("Edit file", "a.py", True)
    assert "-x = 1" in body and "+x = 2" in body


class _Client:
    """Model stub: one write_file tool call, then a final answer."""

    def __init__(self):
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            call = {"function": {"name": "write_file", "arguments": {"path": "out.txt", "content": "hi"}}}
            return {"message": {"content": "", "tool_calls": [call]}}
        return {"message": {"content": "done"}}


def test_run_task_skips_a_declined_tool(tmp_path):
    result = llm.run_task(
        "t", [], _Client(), "m", base_dir=str(tmp_path), approve=lambda name, args: False
    )
    assert not os.path.exists(tmp_path / "out.txt")
    tool_msgs = [m for m in result.history if m["role"] == "tool"]
    assert "declined" in tool_msgs[0]["content"]


def test_run_task_runs_an_approved_tool(tmp_path):
    llm.run_task("t", [], _Client(), "m", base_dir=str(tmp_path), approve=lambda name, args: True)
    assert (tmp_path / "out.txt").read_text() == "hi"


def test_run_task_without_a_hook_runs_tools_as_before(tmp_path):
    llm.run_task("t", [], _Client(), "m", base_dir=str(tmp_path))
    assert (tmp_path / "out.txt").exists()


def test_a_crashing_hook_counts_as_a_refusal(tmp_path):
    def boom(name, args):
        raise RuntimeError("x")

    llm.run_task("t", [], _Client(), "m", base_dir=str(tmp_path), approve=boom)
    assert not os.path.exists(tmp_path / "out.txt")


def test_hook_is_not_consulted_in_read_only_mode(tmp_path):
    asked = []
    llm.run_task(
        "t", [], _Client(), "m", base_dir=str(tmp_path), read_only=True,
        approve=lambda name, args: asked.append(name) or True,
    )
    assert asked == []


def test_no_approval_hook_without_a_terminal(monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)
    assert main._approval_hook("default", set(), object()) is None


def test_no_approval_hook_in_dangerous_mode(monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: True)
    assert main._approval_hook("dangerous", set(), object()) is None


@pytest.mark.parametrize("arg, plan, edit", [("plan", True, "default"), ("auto", False, "auto")])
def test_mode_command_sets_state(arg, plan, edit):
    state = main.REPLState(config=None, client=None)
    main._handle_mode_command(arg, state)
    assert (state.plan_mode, state.edit_mode) == (plan, edit)


def test_mode_command_rejects_unknown_names():
    state = main.REPLState(config=None, client=None)
    assert "Unknown mode" in main._handle_mode_command("yolo", state)
    assert (state.plan_mode, state.edit_mode) == (False, "default")


def test_conflicting_mode_flags_are_an_error(capsys):
    assert main.main(["--plan", "--dangerous-edits", "hello"]) == 2
    assert "conflicting" in capsys.readouterr().err


def test_cycle_goes_default_auto_plan_and_wraps():
    state = main.REPLState(config=None, client=None)
    seen = []
    for _ in range(4):
        main._cycle_mode(state)
        seen.append(modes.label(state.plan_mode, state.edit_mode))
    assert seen == ["auto-edits", "plan", "ask", "auto-edits"]


def test_cycle_leaves_dangerous_for_default_and_never_enters_it():
    state = main.REPLState(config=None, client=None, edit_mode="dangerous")
    main._cycle_mode(state)
    assert (state.plan_mode, state.edit_mode) == (False, "default")
    for _ in range(6):
        main._cycle_mode(state)
        assert state.edit_mode != "dangerous"


def test_shift_tab_calls_the_cycle_hook(monkeypatch, tmp_path):
    from unittest import mock

    from prompt_toolkit import PromptSession
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from forge import ui

    calls = []
    with create_pipe_input() as pipe:
        real = PromptSession
        monkeypatch.setattr("prompt_toolkit.PromptSession", lambda **kw: real(input=pipe, output=DummyOutput(), **kw))
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("sys.stdout.isatty", return_value=True):
            session = ui.make_input(["/help"], str(tmp_path / "hist"), lambda: "status", on_cycle_mode=lambda: calls.append(1))
        pipe.send_text("\x1b[Z\x1b[Zhi\r")
        assert session.prompt("> ") == "hi"
    assert calls == [1, 1]
