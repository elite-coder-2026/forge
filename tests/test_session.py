import json
import os

from forge import main, session
from forge.config import Config

HISTORY = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "hello"},
]


def test_default_path_is_stable_per_directory_and_distinct(tmp_path):
    a, b = tmp_path / "proj", tmp_path / "other"
    assert session.default_path(str(a)) == session.default_path(str(a))
    assert session.default_path(str(a)) != session.default_path(str(b))
    assert session.default_path(str(a)).startswith(session.SESSIONS_DIR)


def test_save_then_load_roundtrip(tmp_path):
    path = str(tmp_path / "s" / "session.json")
    session.save(path, HISTORY)
    assert session.load(path) == HISTORY
    assert not os.path.exists(path + ".tmp")


def test_load_missing_returns_none(tmp_path):
    assert session.load(str(tmp_path / "nope.json")) is None


def test_load_unusable_files_return_none(tmp_path):
    path = tmp_path / "s.json"
    for bad in ["not json", "{}", "[]", '["x"]', '[{"content": "no role"}]']:
        path.write_text(bad)
        assert session.load(str(path)) is None


def test_empty_path_disables_everything():
    session.save("", HISTORY)
    assert session.load("") is None
    session.clear("")


def test_save_serializes_pydantic_like_objects(tmp_path):
    class ToolCall:
        def model_dump(self):
            return {"function": {"name": "read_file"}}

    path = str(tmp_path / "s.json")
    session.save(path, [{"role": "assistant", "content": "", "tool_calls": [ToolCall()]}])
    assert session.load(path)[0]["tool_calls"] == [{"function": {"name": "read_file"}}]


def test_save_falls_back_to_str_for_unknown_objects(tmp_path):
    path = str(tmp_path / "s.json")
    session.save(path, [{"role": "user", "content": object}])
    assert json.loads(open(path).read())[0]["role"] == "user"


def test_save_failure_is_swallowed(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    session.save(str(blocker / "child" / "s.json"), HISTORY)  # must not raise


def test_clear_removes_file_and_tolerates_missing(tmp_path):
    path = str(tmp_path / "s.json")
    session.save(path, HISTORY)
    session.clear(path)
    assert not os.path.exists(path)
    session.clear(path)


def test_config_defaults(monkeypatch, tmp_path):
    assert Config().session_file == ""
    monkeypatch.delenv("FORGE_SESSION_FILE", raising=False)
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))
    assert Config.from_env().session_file == session.default_path(str(tmp_path))
    monkeypatch.setenv("FORGE_SESSION_FILE", "/custom/s.json")
    assert Config.from_env().session_file == "/custom/s.json"


# --- REPL integration -------------------------------------------------------


def _repl(monkeypatch, config, lines, seen_histories):
    inputs = iter(lines)

    def fake_input(*_):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    def fake_run_task(task, history, *args, **kwargs):
        seen_histories.append(list(history))
        new = list(history) + [
            {"role": "user", "content": task},
            {"role": "assistant", "content": "ok"},
        ]
        return type("R", (), {"content": "ok", "history": new})()

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(main.llm, "run_task", fake_run_task)
    main.run_repl(config, client=None)


def _config(tmp_path):
    return Config(
        model="m", working_dir=str(tmp_path), session_file=str(tmp_path / "session.json")
    )


def test_repl_saves_after_task_and_resumes_on_next_start(monkeypatch, tmp_path, capsys):
    config = _config(tmp_path)
    _repl(monkeypatch, config, ["first task"], [])
    assert len(session.load(config.session_file)) == 2

    seen = []
    _repl(monkeypatch, config, ["second task"], seen)
    assert "Resumed previous session (2 messages)" in capsys.readouterr().out
    assert [m["content"] for m in seen[0]] == ["first task", "ok"]
    assert len(session.load(config.session_file)) == 4


def test_repl_clear_deletes_saved_session(monkeypatch, tmp_path):
    config = _config(tmp_path)
    _repl(monkeypatch, config, ["first task", "/clear"], [])
    assert not os.path.exists(config.session_file)

    seen = []
    _repl(monkeypatch, config, ["again"], seen)
    assert seen[0] == []


def test_repl_without_session_file_saves_nothing(monkeypatch, tmp_path, capsys):
    config = Config(model="m", working_dir=str(tmp_path))
    _repl(monkeypatch, config, ["task"], [])
    assert "Resumed" not in capsys.readouterr().out
    assert os.listdir(tmp_path) == []


def test_repl_corrupt_session_starts_fresh(monkeypatch, tmp_path):
    config = _config(tmp_path)
    (tmp_path / "session.json").write_text("garbage")
    seen = []
    _repl(monkeypatch, config, ["task"], seen)
    assert seen[0] == []
