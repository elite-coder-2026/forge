import os

import pytest

from forge import main, routing
from forge.config import Config

MAIN, FAST = "big-model", "fast-model"

QUICK = [
    "fix typo in README",
    "rename variable foo to bar in utils.py",
    "what does this function do",
    "add a docstring to run_task",
    "add a --verbose flag",
]

LARGE = [
    "refactor the auth module to use dependency injection across all files and add tests",
    "implement a REST API with user auth, database models, and tests",
    "update a.py, b.py and c.py to use the new logger",
    "1. add caching\n2. add retries\n3. add metrics",
    "word " * 70,
]


def _choose(task, **kwargs):
    return routing.choose_model(task, MAIN, FAST, **kwargs)


@pytest.mark.parametrize("task", QUICK)
def test_quick_tasks_go_to_fast_model(task):
    assert _choose(task).model == FAST


@pytest.mark.parametrize("task", LARGE)
def test_large_tasks_go_to_main_model(task):
    assert _choose(task).model == MAIN


def test_routing_is_off_without_a_fast_model():
    choice = routing.choose_model("fix typo", MAIN, "")
    assert choice.model == MAIN and choice.reason == ""


def test_routing_is_off_when_fast_equals_main():
    assert routing.choose_model("fix typo", MAIN, MAIN).reason == ""


def test_images_and_plan_mode_always_use_main_model():
    assert _choose("fix typo", images=True).model == MAIN
    assert _choose("fix typo", plan_mode=True).model == MAIN


def test_borderline_task_defaults_to_main():
    choice = _choose("fix the bug in the login flow")
    assert choice.model == MAIN


@pytest.mark.parametrize("last", [MAIN, FAST])
def test_borderline_task_stays_on_the_last_model(last):
    assert _choose("fix the bug in the login flow", last=last).model == last


@pytest.mark.parametrize("last", [MAIN, FAST])
def test_short_follow_ups_stay_on_the_last_model(last):
    assert _choose("yes do it", last=last).model == last
    assert _choose("now the other one", last=last).model == last


def test_short_message_with_a_quick_keyword_still_switches():
    assert _choose("fix typo", last=MAIN).model == FAST


def test_clearly_large_task_escalates_from_fast():
    assert _choose(LARGE[0], last=FAST).model == MAIN


def test_traceback_pushes_towards_main():
    plain = routing.complexity_score("why does this fail")
    with_trace = routing.complexity_score("why does this fail\nTraceback (most recent call last)")
    assert with_trace > plain


def test_score_is_case_insensitive():
    assert routing.complexity_score("REFACTOR everything") == routing.complexity_score(
        "refactor everything"
    )


def test_every_choice_with_routing_on_explains_itself():
    for task in QUICK + LARGE:
        assert _choose(task).reason


# --- config -----------------------------------------------------------------


def test_fast_model_config(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))
    assert Config().fast_model == "" and Config.from_env().fast_model == ""
    (tmp_path / "forge.toml").write_text('fast_model = "file-fast"\n')
    assert Config.from_env().fast_model == "file-fast"
    monkeypatch.setenv("FORGE_FAST_MODEL", "env-fast")
    assert Config.from_env().fast_model == "env-fast"


# --- REPL / one-shot integration --------------------------------------------


class RecordingClient:
    def __init__(self):
        self.models = []

    def chat(self, **kwargs):
        self.models.append(kwargs["model"])
        response = {"message": {"content": "ok"}}
        return iter([response]) if kwargs.get("stream") else response


def _config(tmp_path, fast=FAST):
    return Config(model=MAIN, fast_model=fast, working_dir=str(tmp_path))


def _repl(monkeypatch, config, lines):
    inputs = iter(lines)

    def fake_input(*_):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    client = RecordingClient()
    monkeypatch.setattr("builtins.input", fake_input)
    main.run_repl(config, client)
    return client.models


def test_repl_routes_each_task_and_follow_ups_stay_put(monkeypatch, tmp_path, capsys):
    models = _repl(
        monkeypatch,
        _config(tmp_path),
        ["fix typo in README", "yes do it", LARGE[0], "yes do it", "fix typo in README"],
    )
    assert models == [FAST, FAST, MAIN, MAIN, FAST]
    out = capsys.readouterr().out
    assert f"[auto] {FAST}: quick task" in out
    assert f"[auto] {MAIN}: larger task" in out


def test_repl_without_fast_model_uses_main_and_prints_no_notice(monkeypatch, tmp_path, capsys):
    models = _repl(monkeypatch, _config(tmp_path, fast=""), ["fix typo in README"])
    assert models == [MAIN]
    assert "[auto]" not in capsys.readouterr().out


def test_repl_auto_off_pins_everything_to_main(monkeypatch, tmp_path):
    models = _repl(monkeypatch, _config(tmp_path), ["/auto off", "fix typo in README"])
    assert models == [MAIN]


def test_repl_prints_auto_status_at_startup_only_when_configured(monkeypatch, tmp_path, capsys):
    _repl(monkeypatch, _config(tmp_path), [])
    assert "Auto model selection: on" in capsys.readouterr().out
    _repl(monkeypatch, _config(tmp_path, fast=""), [])
    assert "Auto model selection" not in capsys.readouterr().out


def test_run_once_routes_and_announces_on_stderr(tmp_path, capsys):
    client = RecordingClient()
    assert main.run_once("fix typo in README", _config(tmp_path), client) == 0
    captured = capsys.readouterr()
    assert client.models == [FAST]
    assert f"[auto] {FAST}: quick task" in captured.err
    assert "[auto]" not in captured.out


def test_explicit_model_flag_disables_routing(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("FORGE_FAST_MODEL", FAST)
    seen = {}

    def fake_run_once(task, config, client, plan_mode=False, images=None, edit_mode="default"):
        seen["model"], seen["fast"] = config.model, config.fast_model
        return 0

    monkeypatch.setattr(main, "run_once", fake_run_once)
    main.main(["--model", "pinned", "fix typo"])
    assert seen == {"model": "pinned", "fast": ""}


# --- slash commands ---------------------------------------------------------


def _state(tmp_path, fast=FAST):
    return main.REPLState(config=_config(tmp_path, fast=fast), client=None)


def test_auto_status_and_toggle(tmp_path):
    state = _state(tmp_path)
    assert "on" in main.handle_slash_command("/auto", state)
    assert "off" in main.handle_slash_command("/auto off", state) and not state.auto_model
    assert "on" in main.handle_slash_command("/auto on", state) and state.auto_model


def test_auto_without_fast_model(tmp_path):
    state = _state(tmp_path, fast="")
    assert "not configured" in main.handle_slash_command("/auto", state)
    assert "No fast model set" in main.handle_slash_command("/auto on", state)


def test_auto_rejects_bad_argument(tmp_path):
    assert main.handle_slash_command("/auto maybe", _state(tmp_path)).startswith("Usage:")


def test_fast_command_sets_clears_and_enables(tmp_path):
    state = _state(tmp_path, fast="")
    assert main.handle_slash_command("/fast", state).startswith("Usage:")
    main.handle_slash_command("/fast small-one", state)
    assert state.config.fast_model == "small-one" and state.auto_model
    main.handle_slash_command("/fast off", state)
    assert state.config.fast_model == ""


def test_model_command_turns_auto_off_only_when_routing_was_active(tmp_path):
    active = _state(tmp_path)
    out = main.handle_slash_command("/model other", active)
    assert "auto model selection off" in out and not active.auto_model
    assert active.config.model == "other"

    inactive = _state(tmp_path, fast="")
    assert main.handle_slash_command("/model other", inactive) == "Model set to 'other'"


def test_help_mentions_auto_and_fast():
    assert "/auto" in main.HELP_TEXT and "/fast" in main.HELP_TEXT
