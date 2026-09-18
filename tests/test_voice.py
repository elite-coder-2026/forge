import os
import tempfile

import pytest

from forge import main, voice
from forge.config import Config

RECORD = "printf '%s' 'add a docstring to main' > {audio}"  # stands in for a microphone
TRANSCRIBE = "cat {audio}"  # stands in for whisper: prints the "speech"


def _listen(record=RECORD, transcribe=TRANSCRIBE, seconds=5):
    return voice.listen(record, transcribe, seconds)


# --- transcript cleaning ----------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  hello   world \n", "hello world"),
        ("line one\nline two\n", "line one line two"),
        ("[00:00:00.000 --> 00:00:03.500]   fix the bug", "fix the bug"),
        ("[00:00:00.000 --> 00:00:02.000]  first\n[00:00:02.000 --> 00:00:04.000]  second", "first second"),
        ("[BLANK_AUDIO]", ""),
        ("(music) open the file [silence]", "open the file"),
        ("keep [this bracket] and (this one)", "keep [this bracket] and (this one)"),
        ("", ""),
    ],
)
def test_clean_transcript(raw, expected):
    assert voice.clean_transcript(raw) == expected


# --- recorder detection -----------------------------------------------------


def _which(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


def test_prefers_sox_then_arecord_then_ffmpeg():
    assert voice.find_recorder(_which("rec", "arecord", "ffmpeg")).startswith("rec ")
    assert voice.find_recorder(_which("arecord", "ffmpeg")).startswith("arecord ")
    assert voice.find_recorder(_which("ffmpeg")).startswith("ffmpeg ")


def test_ffmpeg_uses_the_platform_input_device():
    assert "avfoundation" in voice.find_recorder(_which("ffmpeg"), system="Darwin")
    assert "pulse" in voice.find_recorder(_which("ffmpeg"), system="Linux")


def test_no_recorder_installed():
    assert voice.find_recorder(_which()) is None


def test_every_default_recorder_has_the_placeholders():
    for tools in (("rec",), ("arecord",), ("ffmpeg",)):
        command = voice.find_recorder(_which(*tools))
        assert "{audio}" in command and "{seconds}" in command


# --- the pipeline -----------------------------------------------------------


def test_listen_records_then_transcribes():
    assert _listen() == "add a docstring to main"


def test_seconds_placeholder_is_filled_in():
    text = voice.listen("printf '%s' {seconds}s > {audio}", TRANSCRIBE, 12)
    assert text == "12s"


def test_shell_braces_in_commands_are_left_alone():
    assert voice.listen("echo ${HOME:-x} > {audio}", "cat {audio}", 5) == os.environ.get("HOME", "x")


def test_paths_with_spaces_are_quoted(tmp_path, monkeypatch):
    spaced = tmp_path / "my tmp dir"
    spaced.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(spaced))
    assert _listen() == "add a docstring to main"


def test_the_recording_is_deleted_afterwards(tmp_path):
    marker = tmp_path / "path.txt"
    voice.listen(RECORD, f"echo {{audio}} > {marker}; cat {{audio}}", 5)
    recorded_path = marker.read_text().strip()
    assert recorded_path.endswith("clip.wav") and not os.path.exists(recorded_path)


def test_the_recording_is_deleted_even_when_transcription_fails(tmp_path):
    marker = tmp_path / "path.txt"
    with pytest.raises(voice.VoiceError):
        voice.listen(RECORD, f"echo {{audio}} > {marker}; exit 1", 5)
    assert not os.path.exists(marker.read_text().strip())


def test_whisper_style_output_is_cleaned():
    text = voice.listen(
        RECORD, "printf '[00:00:00.000 --> 00:00:02.000]   run the tests\\n'; cat /dev/null # {audio}", 5
    )
    assert text == "run the tests"


# --- failure modes ----------------------------------------------------------


def test_missing_transcriber_explains_how_to_configure_it():
    with pytest.raises(voice.VoiceError, match="voice_transcribe"):
        voice.listen(RECORD, "  ", 5)


def test_no_recorder_available_explains_the_options(monkeypatch):
    monkeypatch.setattr(voice, "find_recorder", lambda *a, **k: None)
    with pytest.raises(voice.VoiceError, match="sox"):
        voice.listen("", TRANSCRIBE, 5)


def test_autodetected_recorder_is_used_when_none_configured(monkeypatch):
    monkeypatch.setattr(voice, "find_recorder", lambda *a, **k: RECORD)
    assert voice.listen("", TRANSCRIBE, 5) == "add a docstring to main"


def test_recorder_failure_reports_its_last_error_line():
    with pytest.raises(voice.VoiceError, match="recording failed: no mic here"):
        voice.listen("echo 'warming up' >&2; echo 'no mic here' >&2; exit 1 # {audio}", TRANSCRIBE, 5)


def test_recorder_failure_without_output_gives_the_exit_code():
    with pytest.raises(voice.VoiceError, match="exit code 3"):
        voice.listen("exit 3 # {audio}", TRANSCRIBE, 5)


def test_recorder_that_writes_nothing():
    with pytest.raises(voice.VoiceError, match="nothing was recorded"):
        voice.listen("true # {audio}", TRANSCRIBE, 5)
    with pytest.raises(voice.VoiceError, match="nothing was recorded"):
        voice.listen(": > {audio}", TRANSCRIBE, 5)  # empty file


def test_recorder_that_never_stops(monkeypatch):
    monkeypatch.setattr(voice, "RECORD_GRACE", 0)
    with pytest.raises(voice.VoiceError, match="didn't stop"):
        voice.listen("sleep 5 # {audio}", TRANSCRIBE, 1)


def test_transcriber_failure_reports_its_last_error_line():
    with pytest.raises(voice.VoiceError, match="transcription failed: model not found"):
        voice.listen(RECORD, "echo 'loading' >&2; echo 'model not found' >&2; exit 2 # {audio}", 5)


def test_transcriber_that_hears_nothing():
    for output in ("", "[BLANK_AUDIO]", "   \n"):
        with pytest.raises(voice.VoiceError, match="didn't catch anything"):
            voice.listen(RECORD, f"printf '%s' '{output}' # {{audio}}", 5)


def test_transcriber_timeout(monkeypatch):
    monkeypatch.setattr(voice, "TRANSCRIBE_TIMEOUT", 1)
    with pytest.raises(voice.VoiceError, match="took longer"):
        voice.listen(RECORD, "sleep 5 # {audio}", 5)


@pytest.mark.parametrize("which", ["record", "transcribe"])
def test_commands_must_reference_the_audio_file(which):
    if which == "record":
        with pytest.raises(voice.VoiceError, match="voice_record must contain"):
            voice.listen("echo hi", TRANSCRIBE, 5)
    else:
        with pytest.raises(voice.VoiceError, match="voice_transcribe must contain"):
            voice.listen(RECORD, "echo hi", 5)


# --- config -----------------------------------------------------------------


def test_voice_config_defaults_env_and_file(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))
    config = Config.from_env()
    assert (config.voice_record, config.voice_transcribe, config.voice_seconds) == ("", "", 30)

    (tmp_path / "forge.toml").write_text(
        'voice_record = "rec {audio}"\nvoice_transcribe = "whisper {audio}"\nvoice_seconds = 10\n'
    )
    config = Config.from_env()
    assert (config.voice_record, config.voice_transcribe, config.voice_seconds) == (
        "rec {audio}", "whisper {audio}", 10,
    )
    monkeypatch.setenv("FORGE_VOICE_SECONDS", "5")
    monkeypatch.setenv("FORGE_VOICE_TRANSCRIBE", "env-cmd {audio}")
    config = Config.from_env()
    assert config.voice_seconds == 5 and config.voice_transcribe == "env-cmd {audio}"


# --- _voice_task ------------------------------------------------------------


def _config(**kwargs):
    kwargs.setdefault("voice_record", RECORD)
    kwargs.setdefault("voice_transcribe", TRANSCRIBE)
    return Config(**kwargs)


def test_voice_task_shows_the_transcript_and_returns_it_on_yes(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert main._voice_task(_config()) == "add a docstring to main"
    out = capsys.readouterr().out
    assert 'Heard: "add a docstring to main"' in out and "Listening (up to 30s" in out


@pytest.mark.parametrize("answer, sent", [("", True), ("Y", True), ("yes", True), ("n", False), ("no", False), ("x", False)])
def test_voice_task_confirmation_answers(monkeypatch, answer, sent):
    monkeypatch.setattr("builtins.input", lambda *_: answer)
    assert (main._voice_task(_config()) is not None) is sent


def test_voice_task_without_confirmation_never_prompts(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: pytest.fail("should not prompt"))
    assert main._voice_task(_config(), confirm=False) == "add a docstring to main"


@pytest.mark.parametrize("exc", [EOFError, KeyboardInterrupt])
def test_voice_task_interrupted_at_the_prompt_is_a_no(monkeypatch, exc):
    def boom(*_):
        raise exc

    monkeypatch.setattr("builtins.input", boom)
    assert main._voice_task(_config()) is None


def test_voice_task_reports_errors_and_returns_none(capsys):
    config = Config(voice_record=RECORD, voice_transcribe="")
    assert main._voice_task(config) is None
    assert "voice_transcribe" in capsys.readouterr().err


def test_voice_task_cancelled_with_ctrl_c_while_recording(monkeypatch, capsys):
    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(main.voice, "listen", interrupted)
    assert main._voice_task(_config()) is None
    assert "Cancelled" in capsys.readouterr().err


def test_voice_seconds_is_shown_and_never_below_one(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    main._voice_task(_config(voice_seconds=0))
    assert "up to 1s" in capsys.readouterr().out


# --- /voice in the REPL -----------------------------------------------------


def _run_repl(monkeypatch, config, lines):
    inputs = iter(lines)
    ran = []

    def fake_input(prompt=""):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    def fake_run_task(task, history, client, model, **kwargs):
        ran.append(task)
        return type("R", (), {"content": "ok", "history": list(history)})()

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(main.llm, "run_task", fake_run_task)
    main.run_repl(config, client=None)
    return ran


def test_repl_voice_command_sends_the_transcript_as_a_task(monkeypatch, tmp_path):
    config = _config(working_dir=str(tmp_path))
    assert _run_repl(monkeypatch, config, ["/voice", "y"]) == ["add a docstring to main"]


def test_repl_declined_voice_task_is_not_run(monkeypatch, tmp_path):
    config = _config(working_dir=str(tmp_path))
    assert _run_repl(monkeypatch, config, ["/voice", "n"]) == []


def test_repl_failed_voice_keeps_the_session_alive(monkeypatch, tmp_path, capsys):
    config = Config(working_dir=str(tmp_path))  # no transcriber configured
    assert _run_repl(monkeypatch, config, ["/voice", "a typed task"]) == ["a typed task"]
    assert "voice_transcribe" in capsys.readouterr().err


def test_a_dictated_slash_command_runs_as_a_task_not_a_command(monkeypatch, tmp_path):
    config = _config(working_dir=str(tmp_path), voice_transcribe="printf '%s' '/clear the cache' # {audio}")
    assert _run_repl(monkeypatch, config, ["/voice", "y"]) == ["/clear the cache"]


# --- --voice on the command line --------------------------------------------


@pytest.fixture
def cli(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))
    monkeypatch.setenv("FORGE_VOICE_RECORD", RECORD)
    monkeypatch.setenv("FORGE_VOICE_TRANSCRIBE", TRANSCRIBE)
    seen = {}

    def fake_run_once(task, config, client, plan_mode=False, images=None):
        seen["task"] = task
        return 0

    monkeypatch.setattr(main, "run_once", fake_run_once)
    return seen


def test_voice_flag_runs_the_dictated_task(cli, monkeypatch, capsys):
    monkeypatch.setattr(main, "_is_interactive", lambda: False)  # piped: no confirmation prompt
    assert main.main(["--voice"]) == 0
    assert cli["task"] == "add a docstring to main"
    assert 'Heard: "add a docstring to main"' in capsys.readouterr().out


def test_voice_flag_confirms_in_a_terminal(cli, monkeypatch):
    monkeypatch.setattr(main, "_is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert main.main(["--voice"]) == 1 and "task" not in cli
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert main.main(["--voice"]) == 0 and cli["task"] == "add a docstring to main"


def test_voice_flag_failure_exits_1(cli, monkeypatch):
    monkeypatch.setenv("FORGE_VOICE_TRANSCRIBE", "")
    assert main.main(["--voice"]) == 1 and "task" not in cli


@pytest.mark.parametrize("argv", [["--voice", "typed", "task"], ["--voice", "-i"]])
def test_voice_flag_conflicts_with_a_typed_task_or_repl(cli, capsys, argv):
    assert main.main(argv) == 2
    assert "--voice" in capsys.readouterr().err and "task" not in cli


def test_help_and_flag_parsing():
    assert "/voice" in main.HELP_TEXT
    assert main.parse_args(["--voice"])[0].voice is True
