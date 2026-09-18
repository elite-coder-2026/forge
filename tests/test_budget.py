import os

import pytest

from forge import budget, llm, main, vision
from forge.config import Config

# --- BudgetTracker ----------------------------------------------------------


def test_disabled_limits_never_warn():
    tracker = budget.BudgetTracker(0, 0)
    assert tracker.check(10**9, 10**9) == []


def test_token_warning_fires_once_when_crossed():
    tracker = budget.BudgetTracker(token_limit=1000)
    assert tracker.check(999, 0) == []
    (notice,) = tracker.check(1000, 0)
    assert "1,000 tokens" in notice and "/clear" in notice
    assert tracker.check(1500, 0) == []  # same multiple: no repeat


def test_token_warning_repeats_at_each_further_multiple():
    tracker = budget.BudgetTracker(token_limit=1000)
    assert len(tracker.check(1000, 0)) == 1
    assert len(tracker.check(2100, 0)) == 1
    assert tracker.check(2900, 0) == []
    assert len(tracker.check(3000, 0)) == 1


def test_a_big_jump_warns_once_not_per_multiple():
    tracker = budget.BudgetTracker(token_limit=1000)
    assert len(tracker.check(5500, 0)) == 1
    assert tracker.check(5900, 0) == []


def test_minutes_warning():
    tracker = budget.BudgetTracker(minutes_limit=15)
    assert tracker.check(0, 14 * 60) == []
    (notice,) = tracker.check(0, 15.5 * 60)
    assert "15.5 minutes" in notice and "soft limit 15" in notice
    assert tracker.check(0, 20 * 60) == []
    assert len(tracker.check(0, 30 * 60)) == 1


def test_both_limits_can_fire_together_and_independently():
    tracker = budget.BudgetTracker(token_limit=100, minutes_limit=1)
    assert len(tracker.check(100, 60)) == 2
    assert tracker.check(150, 90) == []
    assert len(tracker.check(250, 90)) == 1


def test_set_limits_does_not_fire_a_stale_alert():
    tracker = budget.BudgetTracker(token_limit=1000)
    tracker.check(1000, 0)
    tracker.set_limits(tokens=100, used_tokens=1000)  # lowered: already 10x over
    assert tracker.check(1050, 0) == []
    assert len(tracker.check(1100, 0)) == 1


def test_set_limits_off_disables_and_reenabling_starts_from_current_use():
    tracker = budget.BudgetTracker(token_limit=100)
    tracker.set_limits(tokens=0)
    assert tracker.check(10_000, 0) == []
    tracker.set_limits(tokens=1000, used_tokens=10_000)
    assert tracker.check(10_500, 0) == []


def test_status_text():
    tracker = budget.BudgetTracker(token_limit=2000, minutes_limit=0)
    text = tracker.status(1234, 90)
    assert "1,234 of 2,000" in text
    assert "1.5 min (no limit)" in text
    assert "nothing is blocked" in text


# --- llm: compute time and the on_step hook ---------------------------------


class FakeTime:
    """Each monotonic() call advances the clock by `step` seconds."""

    def __init__(self, step):
        self.now = 0.0
        self.step = step

    def monotonic(self):
        self.now += self.step
        return self.now


class Client:
    def __init__(self, prompt=10, completion=5, error=None):
        self.prompt, self.completion, self.error = prompt, completion, error
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        if self.error:
            raise self.error
        response = {
            "message": {"content": "ok"},
            "prompt_eval_count": self.prompt,
            "eval_count": self.completion,
        }
        return iter([response]) if kwargs.get("stream") else response


@pytest.fixture(autouse=True)
def _reset():
    llm.reset_usage()
    yield
    llm.reset_usage()


def test_compute_seconds_accumulate_per_model_call(monkeypatch):
    monkeypatch.setattr(llm, "time", FakeTime(step=30))
    llm.run_task("hi", llm.new_history(), Client(), "m")
    assert llm.get_compute_seconds() == 30


def test_compute_seconds_count_failed_calls_too(monkeypatch):
    monkeypatch.setattr(llm, "time", FakeTime(step=7))
    with pytest.raises(llm.LLMError):
        llm.run_task("hi", llm.new_history(), Client(error=ValueError("x")), "m")
    assert llm.get_compute_seconds() == 7


def test_reset_usage_clears_compute_seconds():
    llm.add_compute_seconds(12)
    llm.reset_usage()
    assert llm.get_compute_seconds() == 0


def test_negative_seconds_are_ignored():
    llm.add_compute_seconds(-5)
    assert llm.get_compute_seconds() == 0


def test_compute_seconds_do_not_leak_into_token_usage_dict():
    llm.add_compute_seconds(5)
    assert set(llm.get_usage()) == {"prompt_tokens", "completion_tokens", "calls"}


def test_on_step_runs_after_usage_is_recorded():
    seen = []
    llm.run_task(
        "hi", llm.new_history(), Client(), "m", on_step=lambda: seen.append(llm.get_usage()["calls"])
    )
    assert seen == [1]


def test_on_step_errors_never_break_the_task():
    def boom():
        raise RuntimeError("bad hook")

    result = llm.run_task("hi", llm.new_history(), Client(), "m", on_step=boom)
    assert result.content == "ok"


def test_on_step_not_called_when_the_model_call_fails():
    seen = []
    with pytest.raises(llm.LLMError):
        llm.run_task(
            "hi", llm.new_history(), Client(error=ValueError("x")), "m", on_step=lambda: seen.append(1)
        )
    assert seen == []


def test_vision_calls_count_toward_compute_time(monkeypatch, tmp_path):
    monkeypatch.setattr(vision, "time", FakeTime(step=20))
    image = tmp_path / "a.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
    vision.describe_images(Client(), "m", [str(image)], "task")
    assert llm.get_compute_seconds() == 20


# --- printer ----------------------------------------------------------------


def test_notice_breaks_a_streamed_line_and_goes_to_stderr(capsys):
    printer = main._LivePrinter()
    printer("partial answer")
    printer.notice("careful")
    printer.finish()
    captured = capsys.readouterr()
    assert captured.out == "partial answer\n"  # no extra blank line after the notice
    assert captured.err == "[budget] careful\n"


def test_notice_before_any_output_adds_no_blank_line(capsys):
    printer = main._LivePrinter()
    printer.notice("early")
    captured = capsys.readouterr()
    assert captured.out == "" and "[budget] early" in captured.err


def test_finish_still_ends_a_normal_streamed_line(capsys):
    printer = main._LivePrinter()
    printer("Hel")
    printer("lo")
    printer.finish()
    assert capsys.readouterr().out == "Hello\n"


# --- config -----------------------------------------------------------------


def test_budget_config_defaults_env_and_file(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))
    config = Config.from_env()
    assert (config.budget_tokens, config.budget_minutes) == (200_000, 15.0)

    (tmp_path / "forge.toml").write_text("budget_tokens = 5000\nbudget_minutes = 2.5\n")
    config = Config.from_env()
    assert (config.budget_tokens, config.budget_minutes) == (5000, 2.5)

    monkeypatch.setenv("FORGE_BUDGET_TOKENS", "0")
    monkeypatch.setenv("FORGE_BUDGET_MINUTES", "1")
    config = Config.from_env()
    assert (config.budget_tokens, config.budget_minutes) == (0, 1.0)


# --- one-shot and REPL integration ------------------------------------------


def _config(tmp_path, tokens=0, minutes=0.0):
    return Config(
        model="m", working_dir=str(tmp_path), budget_tokens=tokens, budget_minutes=minutes
    )


def test_run_once_warns_mid_task_when_token_limit_crossed(tmp_path, capsys):
    main.run_once("hello", _config(tmp_path, tokens=10), Client(prompt=10, completion=5))
    err = capsys.readouterr().err
    assert "[budget]" in err and "15 tokens" in err


def test_run_once_warns_on_compute_minutes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(llm, "time", FakeTime(step=1200))  # 20 min per call
    main.run_once("hello", _config(tmp_path, minutes=15), Client())
    assert "20.0 minutes" in capsys.readouterr().err


def test_run_once_is_silent_under_budget(tmp_path, capsys):
    main.run_once("hello", _config(tmp_path, tokens=10_000, minutes=10), Client())
    assert "[budget]" not in capsys.readouterr().err


def _repl(monkeypatch, config, client, lines):
    inputs = iter(lines)

    def fake_input(*_):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    main.run_repl(config, client)


def test_repl_warns_once_per_crossing_across_tasks(monkeypatch, tmp_path, capsys):
    # 15 tokens per task; limit 40 -> crossed during the 3rd task, not again until 80.
    _repl(monkeypatch, _config(tmp_path, tokens=40), Client(), ["a task", "b task", "c task", "d task"])
    err = capsys.readouterr().err
    assert err.count("[budget]") == 1


def test_repl_without_budget_never_warns(monkeypatch, tmp_path, capsys):
    _repl(monkeypatch, _config(tmp_path), Client(), ["a task", "b task"])
    assert "[budget]" not in capsys.readouterr().err


# --- /budget command --------------------------------------------------------


def _state(tokens=1000, minutes=5.0):
    return main.REPLState(
        config=Config(), client=None, budget=budget.BudgetTracker(tokens, minutes)
    )


def test_budget_command_shows_status_with_session_totals():
    llm._record_usage({"prompt_eval_count": 100, "eval_count": 50})
    llm.add_compute_seconds(90)
    out = main.handle_slash_command("/budget", _state())
    assert "150 of 1,000" in out and "1.5 min of 5 min" in out


def test_budget_command_sets_and_disables_limits():
    state = _state()
    assert "of 5,000" in main.handle_slash_command("/budget tokens 5000", state)
    assert state.budget.token_limit == 5000
    assert "of 2.5 min" in main.handle_slash_command("/budget minutes 2.5", state)
    assert "(no limit)" in main.handle_slash_command("/budget tokens off", state)
    assert state.budget.token_limit == 0
    main.handle_slash_command("/budget minutes 0", state)
    assert state.budget.minutes_limit == 0


@pytest.mark.parametrize(
    "arg",
    ["tokens", "tokens abc", "tokens -5", "bogus 5", "tokens 5 6", "minutes off now"],
)
def test_budget_command_rejects_bad_arguments(arg):
    state = _state()
    assert main.handle_slash_command(f"/budget {arg}", state).startswith("Usage:")
    assert (state.budget.token_limit, state.budget.minutes_limit) == (1000, 5.0)


def test_help_mentions_budget():
    assert "/budget" in main.HELP_TEXT
