import pytest

from forge import llm, main, ui
from forge.config import Config


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


# --- tool events from the loop ------------------------------------------------


def test_tool_callbacks_bracket_a_tool_that_runs(tmp_path):
    events = []
    llm.run_task(
        "t", [], _Client(), "m", base_dir=str(tmp_path),
        on_tool_start=lambda name, args: events.append(("start", name, args["path"])),
        on_tool_result=lambda name, result: events.append(("result", name, result)),
    )
    assert events[0] == ("start", "write_file", "out.txt")
    assert events[1][:2] == ("result", "write_file")


def test_tool_callbacks_are_skipped_when_the_user_declines(tmp_path):
    events = []
    llm.run_task(
        "t", [], _Client(), "m", base_dir=str(tmp_path), approve=lambda n, a: False,
        on_tool_start=lambda *a: events.append("start"),
        on_tool_result=lambda *a: events.append("result"),
    )
    assert events == []


def test_a_failing_tool_callback_never_breaks_the_task(tmp_path):
    def boom(*args):
        raise RuntimeError("display bug")

    result = llm.run_task(
        "t", [], _Client(), "m", base_dir=str(tmp_path), on_tool_start=boom, on_tool_result=boom
    )
    assert result.content == "done"
    assert (tmp_path / "out.txt").exists()


# --- drawing tool blocks ------------------------------------------------------


def test_tool_blocks_draw_nothing_when_output_is_not_a_terminal(capsys):
    ui.render_tool_start("run_shell", "ls")
    ui.render_tool_result("run_shell", "ls", ok=True, output="exit code: 0\nstdout:\nx")
    assert capsys.readouterr().out == ""


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("COLUMNS", "80")


def test_shell_result_shows_trimmed_output_and_exit_code(terminal, capsys):
    body = "\n".join(f"line {i}" for i in range(20))
    ui.render_tool_result("run_shell", "make", ok=True, output=f"exit code: 2\nstdout:\n{body}")
    out = capsys.readouterr().out
    assert "✗" in out and "exit code 2" in out
    assert "line 19" in out and "line 0" not in out and "earlier lines" in out


def test_successful_command_is_marked_done(terminal, capsys):
    ui.render_tool_result("run_shell", "true", ok=False, output="exit code: 0")
    assert "✓" in capsys.readouterr().out  # the exit code decides, not the flag


def test_edit_result_can_show_a_diff_panel(terminal, capsys):
    ui.render_tool_result("edit_file", "a.py", ok=True, output="ok", diff="@@ -1 +1 @@\n-x = 1\n+x = 2")
    out = capsys.readouterr().out
    assert "✓" in out and "-x = 1" in out and "+x = 2" in out


def test_failed_tool_shows_the_error_line(terminal, capsys):
    ui.render_tool_result("read_file", "nope", ok=False, output="Error: no such file\nmore")
    out = capsys.readouterr().out
    assert "✗" in out and "Error: no such file" in out and "more" not in out


def test_diff_panel_is_skipped_for_an_empty_diff(terminal, capsys):
    ui.render_diff("a.py", "  \n")
    assert capsys.readouterr().out == ""


# --- Ctrl+C cancels only the turn --------------------------------------------


def _repl(monkeypatch, tmp_path, lines, run_task):
    inputs = iter(lines)

    def fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(main.llm, "run_task", run_task)
    config = Config(session_file=str(tmp_path / "s.json"), usage_file=str(tmp_path / "u.json"))
    main.run_repl(config, client=None)


def test_ctrl_c_during_a_task_cancels_the_turn_and_keeps_the_repl(monkeypatch, tmp_path, capsys):
    tasks = []

    def run_task(task, history, client, model, **kwargs):
        tasks.append(task)
        if len(tasks) == 1:
            raise KeyboardInterrupt
        return type("R", (), {"content": "ok", "history": list(history)})()

    _repl(monkeypatch, tmp_path, ["first", "second"], run_task)
    assert tasks == ["first", "second"]  # the REPL survived and took the next task
    assert "Cancelled." in capsys.readouterr().out


def test_ctrl_c_at_a_plain_prompt_still_exits(monkeypatch, tmp_path):
    def fake_input(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", fake_input)
    config = Config(session_file=str(tmp_path / "s.json"), usage_file=str(tmp_path / "u.json"))
    main.run_repl(config, client=None)  # returns instead of raising or looping forever
