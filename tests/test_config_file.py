import os

import pytest

from forge import main
from forge.config import Config, ConfigError, load_project_file


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))


def _write(tmp_path, text):
    (tmp_path / "forge.toml").write_text(text)


def test_no_file_gives_defaults(tmp_path):
    assert load_project_file(str(tmp_path)) == {}
    config = Config.from_env()
    assert config.model == "qwen2.5-coder"
    assert config.max_iterations == 25


def test_file_values_are_used(tmp_path):
    _write(
        tmp_path,
        'model = "file-model"\nhost = "http://h:1"\nmax_iterations = 40\n'
        "shell_timeout = 5\nprompt_price_per_1m = 1\ncompletion_price_per_1m = 2.5\n",
    )
    config = Config.from_env()
    assert config.model == "file-model"
    assert config.host == "http://h:1"
    assert config.max_iterations == 40
    assert config.shell_timeout == 5
    assert config.prompt_price_per_1m == 1
    assert config.completion_price_per_1m == 2.5


def test_env_overrides_file(tmp_path, monkeypatch):
    _write(tmp_path, 'model = "file-model"\nmax_iterations = 40\n')
    monkeypatch.setenv("FORGE_MODEL", "env-model")
    config = Config.from_env()
    assert config.model == "env-model"
    assert config.max_iterations == 40  # not overridden, still from file


def test_relative_paths_resolve_against_project(tmp_path):
    _write(tmp_path, 'usage_file = "meta/usage.json"\nsession_file = "meta/s.json"\n')
    config = Config.from_env()
    assert config.usage_file == str(tmp_path / "meta" / "usage.json")
    assert config.session_file == str(tmp_path / "meta" / "s.json")


def test_absolute_and_home_paths_are_kept(tmp_path):
    _write(tmp_path, 'usage_file = "/abs/usage.json"\nsession_file = "~/s.json"\n')
    config = Config.from_env()
    assert config.usage_file == "/abs/usage.json"
    assert config.session_file == os.path.expanduser("~/s.json")


def test_invalid_toml_raises(tmp_path):
    _write(tmp_path, "model = = oops")
    with pytest.raises(ConfigError, match="invalid TOML"):
        Config.from_env()


def test_unknown_key_raises_and_lists_valid_keys(tmp_path):
    _write(tmp_path, 'modle = "typo"\n')
    with pytest.raises(ConfigError) as exc:
        Config.from_env()
    assert "modle" in str(exc.value) and "max_iterations" in str(exc.value)


@pytest.mark.parametrize(
    "line",
    ["max_iterations = \"ten\"", "max_iterations = true", "model = 5", "shell_timeout = 1.5"],
)
def test_wrong_type_raises(tmp_path, line):
    _write(tmp_path, line + "\n")
    with pytest.raises(ConfigError, match="must be"):
        Config.from_env()


def test_unreadable_file_raises(tmp_path):
    (tmp_path / "forge.toml").mkdir()  # a directory where the file should be
    with pytest.raises(ConfigError, match="could not be read"):
        Config.from_env()


def test_main_reports_bad_config_and_exits_2(tmp_path, capsys):
    _write(tmp_path, "bogus = 1\n")
    assert main.main(["some task"]) == 2
    err = capsys.readouterr().err
    assert "Error:" in err and "bogus" in err


def test_cli_flag_overrides_file(tmp_path, monkeypatch):
    _write(tmp_path, 'model = "file-model"\nhost = "http://file:1"\n')
    seen = {}

    def fake_run_once(task, config, client, plan_mode=False, images=None, edit_mode="default"):
        seen["model"], seen["host"] = config.model, config.host
        return 0

    monkeypatch.setattr(main, "run_once", fake_run_once)
    main.main(["--model", "flag-model", "task"])
    assert seen == {"model": "flag-model", "host": "http://file:1"}
