import os
import shlex
import tempfile

import pytest

from forge import main, voice
from forge.config import Config

@pytest.fixture(autouse=True)
def _no_builtin_engine_by_default(monkeypatch):
    """The built-in engine is installed in this venv; most tests mean
    "nothing configured and nothing to fall back on"."""
    monkeypatch.setattr(voice, "builtin_available", lambda: False)


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
    def interrupted(*args, **kwargs):
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

    def fake_run_once(task, config, client, plan_mode=False, images=None, edit_mode="default"):
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


# --- the built-in transcriber fallback --------------------------------------


def test_resolve_transcriber_prefers_the_configured_command(monkeypatch):
    monkeypatch.setattr(voice, "builtin_available", lambda: True)
    assert voice.resolve_transcriber("  my-stt {audio}  ") == "my-stt {audio}"


def test_resolve_transcriber_falls_back_to_the_builtin_engine(monkeypatch):
    monkeypatch.setattr(voice, "builtin_available", lambda: True)
    command = voice.resolve_transcriber("")
    assert command == voice.builtin_command()
    assert "-m forge.transcribe {audio}" in command and command.startswith(("/", "'"))


def test_resolve_transcriber_without_any_engine_names_both_options():
    with pytest.raises(voice.VoiceError) as exc:
        voice.resolve_transcriber("")
    assert 'forge[voice]' in str(exc.value) and "voice_transcribe" in str(exc.value)


def test_listen_uses_the_builtin_engine_when_nothing_is_configured(monkeypatch):
    monkeypatch.setattr(voice, "builtin_available", lambda: True)
    monkeypatch.setattr(voice, "builtin_command", lambda: "cat {audio}")
    monkeypatch.setattr(voice, "_builtin_model_cached", lambda: True)
    assert voice.listen(RECORD, "", 5) == "add a docstring to main"


def test_first_use_downloads_the_model_before_listening(monkeypatch):
    events = []
    monkeypatch.setattr(voice, "builtin_available", lambda: True)
    monkeypatch.setattr(voice, "builtin_command", lambda: "cat {audio}")
    monkeypatch.setattr(voice, "_builtin_model_cached", lambda: False)
    monkeypatch.setattr(voice, "_prefetch_builtin_model", lambda: events.append("prefetch"))
    voice.listen(
        "printf '%s' spoken > {audio}; true", "", 5, on_status=lambda m: events.append(m.split(" ")[0].rstrip("."))
    )
    assert events == ["First", "prefetch", "Listening", "Transcribing"]


def test_a_cached_model_or_a_configured_command_never_downloads(monkeypatch):
    monkeypatch.setattr(voice, "_prefetch_builtin_model", lambda: pytest.fail("must not download"))
    monkeypatch.setattr(voice, "builtin_available", lambda: True)
    monkeypatch.setattr(voice, "builtin_command", lambda: "cat {audio}")
    monkeypatch.setattr(voice, "_builtin_model_cached", lambda: True)
    _listen(transcribe="")
    monkeypatch.setattr(voice, "_builtin_model_cached", lambda: False)
    _listen(transcribe=TRANSCRIBE)  # the user's own engine: not our download to make


def test_prefetch_failure_is_a_clear_error(monkeypatch):
    class Result:
        returncode = 1

    monkeypatch.setattr(voice.subprocess, "run", lambda *a, **k: Result())
    with pytest.raises(voice.VoiceError, match="internet connection"):
        voice._prefetch_builtin_model()
    Result.returncode = 0
    voice._prefetch_builtin_model()  # no error


def test_prefetch_when_python_cannot_start(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("no such interpreter")

    monkeypatch.setattr(voice.subprocess, "run", broken)
    with pytest.raises(voice.VoiceError, match="could not start"):
        voice._prefetch_builtin_model()


def test_model_cache_detection(monkeypatch, tmp_path):
    import huggingface_hub

    monkeypatch.delenv("FORGE_WHISPER_MODEL", raising=False)
    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lambda repo, name: "/cache/model.bin")
    assert voice._builtin_model_cached() is True
    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lambda repo, name: None)
    assert voice._builtin_model_cached() is False

    def broken(repo, name):
        raise RuntimeError("cache unreadable")

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", broken)
    assert voice._builtin_model_cached() is True  # unsure: never block

    monkeypatch.setattr(huggingface_hub, "try_to_load_from_cache", lambda repo, name: None)
    monkeypatch.setenv("FORGE_WHISPER_MODEL", str(tmp_path))  # a local model directory
    assert voice._builtin_model_cached() is True
    monkeypatch.setenv("FORGE_WHISPER_MODEL", "someone/custom-model")
    assert voice._builtin_model_cached() is True


# --- Ctrl+C ends a recording early, keeping what was said -------------------


class InterruptedProc:
    """Stands in for a recorder that got Ctrl+C: it finalizes its file, and
    Python's communicate() raises KeyboardInterrupt the first time."""

    def __init__(self, audio_bytes, returncode=255, second_interrupt=False):
        self.audio_bytes, self.returncode, self.second_interrupt = audio_bytes, returncode, second_interrupt
        self.calls = 0
        self.killed = False
        self.audio = None

    def communicate(self, timeout=None):
        self.calls += 1
        if self.calls == 1:
            raise KeyboardInterrupt
        if self.calls == 2 and self.second_interrupt:
            raise KeyboardInterrupt
        return "", ""

    def kill(self):
        self.killed = True


def _fake_popen(monkeypatch, proc, audio_bytes):
    """Only the recorder (marked FAKE-RECORDER) is faked; every other
    process, such as the transcriber, still runs for real."""
    real_popen = voice.subprocess.Popen

    def popen(command, **kwargs):
        if "FAKE-RECORDER" not in command:
            return real_popen(command, **kwargs)
        path = shlex.split(command)[1]  # the {audio} path
        with open(path, "wb") as f:  # what a real recorder does: the file is already on disk
            f.write(audio_bytes)
        return proc

    monkeypatch.setattr(voice.subprocess, "Popen", popen)


def test_ctrl_c_keeps_the_recording_and_reports_it(monkeypatch, tmp_path):
    proc = InterruptedProc(b"RIFFdata")
    _fake_popen(monkeypatch, proc, b"RIFFdata")
    audio = str(tmp_path / "clip.wav")
    assert voice.record("rec {audio} # FAKE-RECORDER", audio, 5) is True
    assert proc.calls == 2 and not proc.killed  # waited for it to finish the file


def test_a_normal_finish_reports_not_interrupted(tmp_path):
    audio = str(tmp_path / "clip.wav")
    assert voice.record("printf x > {audio}", audio, 5) is False


def test_a_second_ctrl_c_kills_the_recorder_but_keeps_the_file(monkeypatch, tmp_path):
    proc = InterruptedProc(b"RIFFdata", second_interrupt=True)
    _fake_popen(monkeypatch, proc, b"RIFFdata")
    assert voice.record("rec {audio} # FAKE-RECORDER", str(tmp_path / "clip.wav"), 5) is True
    assert proc.killed


def test_ctrl_c_before_anything_was_recorded_is_an_error(monkeypatch, tmp_path):
    proc = InterruptedProc(b"")
    _fake_popen(monkeypatch, proc, b"")
    with pytest.raises(voice.VoiceError, match="nothing was recorded"):
        voice.record("rec {audio} # FAKE-RECORDER", str(tmp_path / "clip.wav"), 5)


def test_listen_transcribes_after_an_early_stop(monkeypatch):
    proc = InterruptedProc(b"add a docstring to main")
    _fake_popen(monkeypatch, proc, b"add a docstring to main")
    assert voice.listen("rec {audio} # FAKE-RECORDER", TRANSCRIBE, 5) == "add a docstring to main"


def test_status_messages_tell_the_user_what_to_do():
    messages = []
    voice.listen(RECORD, TRANSCRIBE, 7, on_status=messages.append)
    assert messages == ["Listening (up to 7s). Press Ctrl+C when you're done speaking...", "Transcribing..."]


# --- forge/transcribe.py ----------------------------------------------------


class FakeSegment:
    def __init__(self, text):
        self.text = text


class FakeModel:
    def transcribe(self, path, **kwargs):
        self.kwargs = kwargs
        return [FakeSegment(" add a "), FakeSegment("docstring ")], object()


def test_transcribe_file_joins_segments_and_uses_voice_activity_detection(monkeypatch):
    from forge import transcribe

    model = FakeModel()
    monkeypatch.setattr(transcribe, "_load", lambda name: model)
    assert transcribe.transcribe_file("clip.wav") == "add a docstring"
    assert model.kwargs["vad_filter"] is True


def test_transcribe_model_name_comes_from_the_environment(monkeypatch):
    from forge import transcribe

    monkeypatch.delenv("FORGE_WHISPER_MODEL", raising=False)
    assert transcribe.model_name() == "base.en"
    monkeypatch.setenv("FORGE_WHISPER_MODEL", "small.en")
    assert transcribe.model_name() == "small.en"
    monkeypatch.setenv("FORGE_WHISPER_MODEL", "  ")
    assert transcribe.model_name() == "base.en"


def test_transcribe_cli(monkeypatch, capsys):
    from forge import transcribe

    monkeypatch.setattr(transcribe, "_load", lambda name: FakeModel())
    assert transcribe.main(["clip.wav"]) == 0
    assert capsys.readouterr().out.strip() == "add a docstring"
    assert transcribe.main(["--prefetch"]) == 0
    assert transcribe.main([]) == 2 and transcribe.main(["a", "b"]) == 2


def test_transcribe_cli_reports_a_missing_package_and_other_failures(monkeypatch, capsys):
    from forge import transcribe

    def no_package(name):
        raise ImportError("faster_whisper")

    monkeypatch.setattr(transcribe, "_load", no_package)
    assert transcribe.main(["clip.wav"]) == 1
    assert 'pip install "forge[voice]"' in capsys.readouterr().err

    def broken(name):
        raise RuntimeError("corrupt model")

    monkeypatch.setattr(transcribe, "_load", broken)
    assert transcribe.main(["clip.wav"]) == 1
    assert "RuntimeError: corrupt model" in capsys.readouterr().err


# --- the real thing: macOS speech -> built-in engine (skipped elsewhere) ----


def _real_engine_ready():
    import platform

    if platform.system() != "Darwin" or not (shutil_which("say") and shutil_which("ffmpeg")):
        return False
    try:
        import importlib.util

        if importlib.util.find_spec("faster_whisper") is None:
            return False
        from forge.transcribe import model_name

        from huggingface_hub import try_to_load_from_cache

        return isinstance(try_to_load_from_cache(f"Systran/faster-whisper-{model_name()}", "model.bin"), str)
    except Exception:
        return False


def shutil_which(name):
    import shutil

    return shutil.which(name)


@pytest.mark.skipif(not _real_engine_ready(), reason="needs macOS say + ffmpeg + a downloaded faster-whisper model")
def test_real_speech_is_transcribed_by_the_builtin_engine(monkeypatch, tmp_path):
    import subprocess

    monkeypatch.setattr(voice, "builtin_available", lambda: True)
    spoken = tmp_path / "speech.wav"
    subprocess.run(["say", "-o", str(tmp_path / "s.aiff"), "run the tests and fix the failures"], check=True)
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-i", str(tmp_path / "s.aiff"), "-ac", "1", "-ar", "16000", str(spoken)],
        check=True,
    )
    text = voice.listen(f"cp {spoken} " + "{audio}", "", 10)
    assert "tests" in text.lower() and "failures" in text.lower()
