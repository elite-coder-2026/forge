"""The `think` setting: config parsing and what reaches the Ollama client."""

import pytest

from forge import llm
from forge.config import Config, ConfigError


class RecordingClient:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {"message": {"content": "done"}}


def test_think_defaults_to_the_model_default(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_THINK", raising=False)
    config = Config.from_env(str(tmp_path))
    assert config.think == "" and config.think_setting is None


@pytest.mark.parametrize("value, expected", [("off", False), ("on", True), ("OFF", False)])
def test_think_from_env(tmp_path, monkeypatch, value, expected):
    monkeypatch.setenv("FORGE_THINK", value)
    assert Config.from_env(str(tmp_path)).think_setting is expected


def test_think_from_forge_toml(tmp_path, monkeypatch):
    monkeypatch.delenv("FORGE_THINK", raising=False)
    (tmp_path / "forge.toml").write_text('think = "off"\n')
    assert Config.from_env(str(tmp_path)).think_setting is False


def test_env_beats_forge_toml(tmp_path, monkeypatch):
    (tmp_path / "forge.toml").write_text('think = "off"\n')
    monkeypatch.setenv("FORGE_THINK", "on")
    assert Config.from_env(str(tmp_path)).think_setting is True


def test_invalid_think_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_THINK", "maybe")
    with pytest.raises(ConfigError):
        Config.from_env(str(tmp_path))


def test_think_is_only_sent_when_set():
    unset, off = RecordingClient(), RecordingClient()
    llm.run_task("hi", [], unset, "m")
    llm.run_task("hi", [], off, "m", think=False)
    assert "think" not in unset.calls[0]
    assert off.calls[0]["think"] is False
