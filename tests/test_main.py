import types

import pytest

import forge
import forge.main as main_module
from forge import llm
from forge.config import Config
from forge.main import REPLExit, REPLState, handle_slash_command, parse_args


# ---------------------------------------------------------------------------
# import forge.main (bug #2 regression)
# ---------------------------------------------------------------------------


def test_forge_main_is_a_module_not_shadowed_by_the_function():
    """Regression test for bug #2: `forge/__init__.py` used to do
    `from .main import main`, which bound the name `main` on the `forge`
    package to the *function*, shadowing the `forge.main` submodule.
    `import forge.main` then handed back a function instead of a module.
    """
    assert isinstance(main_module, types.ModuleType)
    assert callable(main_module.main)
    # The package itself must not have re-exported the function under
    # a name that collides with the submodule.
    assert isinstance(forge.main, types.ModuleType)


# ---------------------------------------------------------------------------
# parse_args (bug #1 regression)
# ---------------------------------------------------------------------------


def test_parse_args_dash_prefixed_single_word_task_does_not_crash():
    """Regression test for bug #1: `forge -fix` used to crash argparse
    because `-fix` looked like an unrecognized option to a plain
    `nargs="?"` positional.
    """
    args, task = parse_args(["-fix"])
    assert task == "-fix"


def test_parse_args_multi_word_task():
    args, task = parse_args(["fix", "the", "bug"])
    assert task == "fix the bug"


def test_parse_args_recognized_flags_are_not_swallowed_into_task():
    args, task = parse_args(["--model", "llama3", "fix", "it"])
    assert args.model == "llama3"
    assert task == "fix it"


def test_parse_args_no_task_is_none():
    args, task = parse_args([])
    assert task is None


def test_parse_args_interactive_flag():
    args, task = parse_args(["-i"])
    assert args.interactive is True
    assert task is None


def test_parse_args_dash_prefixed_multi_word_task():
    args, task = parse_args(["-fix", "the", "bug", "please"])
    assert task == "-fix the bug please"


# ---------------------------------------------------------------------------
# handle_slash_command
# ---------------------------------------------------------------------------


def make_state(usage_file=None):
    config = Config(model="initial-model")
    if usage_file is not None:
        config.usage_file = usage_file
    return REPLState(config=config, client=object(), history=[{"role": "user", "content": "hi"}])


def test_slash_clear_resets_history():
    state = make_state()
    assert state.history != []
    output = handle_slash_command("/clear", state)
    assert state.history == []
    assert "cleared" in output.lower()


def test_slash_model_sets_model():
    state = make_state()
    output = handle_slash_command("/model llama3", state)
    assert state.config.model == "llama3"
    assert "llama3" in output


def test_slash_model_with_no_argument_does_not_crash():
    state = make_state()
    output = handle_slash_command("/model", state)
    assert state.config.model == "initial-model"  # unchanged
    assert "Usage" in output


def test_slash_model_with_only_whitespace_does_not_crash():
    state = make_state()
    output = handle_slash_command("/model    ", state)
    assert state.config.model == "initial-model"
    assert "Usage" in output


def test_slash_exit_raises_repl_exit():
    state = make_state()
    with pytest.raises(REPLExit):
        handle_slash_command("/exit", state)


def test_slash_quit_raises_repl_exit():
    state = make_state()
    with pytest.raises(REPLExit):
        handle_slash_command("/quit", state)


def test_slash_help_returns_help_text():
    state = make_state()
    output = handle_slash_command("/help", state)
    assert "/clear" in output
    assert "/model" in output
    assert "/pull" in output


def test_slash_usage_reports_zero_before_any_task(tmp_path):
    llm.reset_usage()
    state = make_state(usage_file=str(tmp_path / "usage.json"))
    output = handle_slash_command("/usage", state)
    assert "calls: 0" in output
    assert "prompt: 0" in output


def test_slash_usage_reports_session_and_all_time_counts(tmp_path):
    llm.reset_usage()
    usage_file = str(tmp_path / "usage.json")
    llm._record_usage({"prompt_eval_count": 10, "eval_count": 4}, usage_file)

    state = make_state(usage_file=usage_file)
    output = handle_slash_command("/usage", state)

    assert "This session" in output
    assert "prompt: 10" in output
    assert "completion: 4" in output
    assert "All-time" in output
    assert "Estimated money saved" in output
    llm.reset_usage()


def test_slash_usage_all_time_survives_session_reset(tmp_path):
    """The whole point of the persisted total: it must outlive
    `llm.reset_usage()` (which only clears the in-process counter).
    """
    llm.reset_usage()
    usage_file = str(tmp_path / "usage.json")
    llm._record_usage({"prompt_eval_count": 100, "eval_count": 50}, usage_file)
    llm.reset_usage()  # simulates a new process / new session

    state = make_state(usage_file=usage_file)
    output = handle_slash_command("/usage", state)

    assert "This session" in output
    assert "calls: 0" in output.split("All-time")[0]
    assert "prompt: 100" in output


def test_slash_pull_with_no_argument_does_not_crash():
    state = make_state()
    output = handle_slash_command("/pull", state)
    assert "Usage" in output


def test_slash_pull_success(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(main_module.subprocess, "run", fake_run)

    state = make_state()
    output = handle_slash_command("/pull llama3", state)

    assert calls == [["ollama", "pull", "llama3"]]
    assert "llama3" in output
    assert not output.startswith("Error")


def test_slash_pull_nonzero_exit_reports_error_not_raise(monkeypatch):
    monkeypatch.setattr(
        main_module.subprocess, "run", lambda cmd: types.SimpleNamespace(returncode=1)
    )

    state = make_state()
    output = handle_slash_command("/pull bogus-model", state)

    assert output.startswith("Error")


def test_slash_pull_ollama_not_installed_does_not_crash(monkeypatch):
    def fake_run(cmd):
        raise FileNotFoundError("ollama not found")

    monkeypatch.setattr(main_module.subprocess, "run", fake_run)

    state = make_state()
    output = handle_slash_command("/pull llama3", state)

    assert output.startswith("Error")
    assert "not found" in output


def test_unknown_slash_command_does_not_crash():
    state = make_state()
    output = handle_slash_command("/bogus", state)
    assert "Unknown command" in output


# ---------------------------------------------------------------------------
# run_repl (scripted input, no real Ollama)
# ---------------------------------------------------------------------------


def test_run_repl_runs_task_and_exits_cleanly(monkeypatch, capsys):
    inputs = iter(["hello there", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    def fake_run_task(task, history, client, model, **kwargs):
        new_history = history + [{"role": "assistant", "content": f"did: {task}"}]
        return llm.TaskResult(content=f"did: {task}", history=new_history, iterations=1)

    monkeypatch.setattr(main_module.llm, "run_task", fake_run_task)

    config = Config(model="test-model")
    main_module.run_repl(config, client=object())

    out = capsys.readouterr().out
    assert "did: hello there" in out


def test_run_repl_model_switch_affects_subsequent_task(monkeypatch, capsys):
    inputs = iter(["/model llama3", "go", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    seen_models = []

    def fake_run_task(task, history, client, model, **kwargs):
        seen_models.append(model)
        return llm.TaskResult(content="ok", history=history, iterations=1)

    monkeypatch.setattr(main_module.llm, "run_task", fake_run_task)

    config = Config(model="original-model")
    main_module.run_repl(config, client=object())

    assert seen_models == ["llama3"]


def test_run_repl_llm_error_is_reported_and_repl_continues(monkeypatch, capsys):
    inputs = iter(["will fail", "/exit"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))

    def fake_run_task(task, history, client, model, **kwargs):
        raise llm.LLMError("backend unreachable")

    monkeypatch.setattr(main_module.llm, "run_task", fake_run_task)

    config = Config(model="test-model")
    main_module.run_repl(config, client=object())  # must not raise

    out = capsys.readouterr().out
    assert "backend unreachable" in out


def test_run_repl_handles_eof_gracefully(monkeypatch, capsys):
    def raise_eof(prompt=""):
        raise EOFError()

    monkeypatch.setattr("builtins.input", raise_eof)

    config = Config(model="test-model")
    main_module.run_repl(config, client=object())  # must not raise
