"""Voice input: dictate a task instead of typing it.

Local and tool-agnostic. forge records a short clip with whatever recorder
is installed (sox, arecord or ffmpeg, or your own command), then hands the
file to a speech-to-text command you configure and reads its stdout. Nothing
is sent to any service by forge itself; what your transcriber does is up to
you (whisper.cpp keeps everything on your machine).

Both commands are templates: `{audio}` is the WAV path, and `{seconds}` (in
the recorder) is the maximum length. With no `voice_transcribe` set, forge
falls back to its built-in offline transcriber (forge/transcribe.py) when
the optional `faster-whisper` package is installed.
"""

from __future__ import annotations

import importlib.util
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Callable

RECORD_GRACE = 15  # seconds a recorder may run past the requested length
TRANSCRIBE_TIMEOUT = 300

# whisper.cpp prints "[00:00:00.000 --> 00:00:03.000]  text" without -nt.
_TIMESTAMP = re.compile(r"\[\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?\s*-->\s*[^\]]*\]")
# Markers transcribers emit for silence or noise, e.g. [BLANK_AUDIO], (music).
_NOISE = re.compile(r"[\[(][^\])]*(?:blank_audio|silence|music|noise|inaudible)[^\])]*[\])]", re.IGNORECASE)


class VoiceError(Exception):
    """Recording or transcription failed, with a message meant for the user."""


def builtin_available() -> bool:
    return importlib.util.find_spec("faster_whisper") is not None


def builtin_command() -> str:
    return f"{shlex.quote(sys.executable)} -m forge.transcribe {{audio}}"


def resolve_transcriber(configured: str) -> str:
    """The user's command, else the built-in one, else a helpful error."""
    if configured.strip():
        return configured.strip()
    if builtin_available():
        return builtin_command()
    raise VoiceError(
        "voice input needs a speech-to-text engine. Easiest: pip install "
        '"forge[voice]" (offline, no other setup). Or set voice_transcribe in forge.toml '
        "(or FORGE_VOICE_TRANSCRIBE) to your own command, for example: "
        'voice_transcribe = "whisper-cli -m ~/models/ggml-base.en.bin -f {audio} -nt"'
    )


def _builtin_model_cached() -> bool:
    """Whether the built-in model is already downloaded. Unsure counts as
    yes, so a quirk here can only skip the friendly notice, never block."""
    from .transcribe import model_name

    name = model_name()
    if os.path.isdir(name) or "/" in name:  # a local path or a custom repo
        return True
    try:
        from huggingface_hub import try_to_load_from_cache

        found = try_to_load_from_cache(f"Systran/faster-whisper-{name}", "model.bin")
        return isinstance(found, str)
    except Exception:  # noqa: BLE001
        return True


def _prefetch_builtin_model() -> None:
    """Download the model up front, with visible progress, so it never
    happens silently in the middle of a dictation."""
    try:
        result = subprocess.run([sys.executable, "-m", "forge.transcribe", "--prefetch"])
    except OSError as e:
        raise VoiceError(f"could not start the transcriber: {e}")
    if result.returncode != 0:
        raise VoiceError("could not download the speech model. Check your internet connection and try again.")


def find_recorder(
    which: Callable[[str], str | None] = shutil.which, system: str | None = None
) -> str | None:
    """A recorder command template for whatever's installed, or None.

    sox stops by itself after ~2s of silence, which makes dictation feel
    natural; the others record for the full length.
    """
    system = system or platform.system()
    if which("rec"):  # part of sox
        return "rec -q -r 16000 -c 1 -b 16 {audio} silence 1 0.1 3% 1 2.0 3% trim 0 {seconds}"
    if which("arecord"):
        return "arecord -q -f S16_LE -r 16000 -c 1 -d {seconds} {audio}"
    if which("ffmpeg"):
        source = "-f avfoundation -i :0" if system == "Darwin" else "-f pulse -i default"
        return f"ffmpeg -loglevel error -y {source} -t {{seconds}} -ac 1 -ar 16000 {{audio}}"
    return None


def _fill(template: str, audio: str, seconds: int | None = None) -> str:
    """Substitute placeholders. Plain replacement, not str.format, so shell
    braces in the command are left alone; the path is shell-quoted."""
    command = template.replace("{audio}", shlex.quote(audio))
    if seconds is not None:
        command = command.replace("{seconds}", str(int(seconds)))
    return command


def clean_transcript(text: str) -> str:
    """Strip timestamps and silence markers; join lines into one sentence."""
    text = _TIMESTAMP.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return " ".join(text.split())


def record(template: str, audio: str, seconds: int) -> bool:
    """Run the recorder. Returns True if Ctrl+C ended it early.

    A terminal's Ctrl+C reaches the recorder too, and sox/arecord/ffmpeg all
    finish the file properly on it, so it means "I'm done speaking" rather
    than "throw it away".
    """
    if "{audio}" not in template:
        raise VoiceError("voice_record must contain {audio} (where the recording is written)")
    proc = subprocess.Popen(
        _fill(template, audio, seconds),
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    interrupted = False
    try:
        out, err = proc.communicate(timeout=seconds + RECORD_GRACE)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise VoiceError("the recorder didn't stop in time")
    except KeyboardInterrupt:
        interrupted = True
        try:
            out, err = proc.communicate(timeout=5)  # let it finalize the file
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            proc.kill()
            out, err = proc.communicate()

    if proc.returncode != 0 and not interrupted:
        detail = (err or out or "").strip().splitlines()
        raise VoiceError(
            "recording failed" + (f": {detail[-1]}" if detail else f" (exit code {proc.returncode})")
            + ". Check microphone access for your terminal."
        )
    if not os.path.isfile(audio) or os.path.getsize(audio) == 0:
        raise VoiceError("nothing was recorded. Check microphone access for your terminal.")
    return interrupted


def transcribe(template: str, audio: str) -> str:
    if "{audio}" not in template:
        raise VoiceError("voice_transcribe must contain {audio} (the recording to transcribe)")
    try:
        result = subprocess.run(
            _fill(template, audio),
            shell=True,
            capture_output=True,
            text=True,
            timeout=TRANSCRIBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise VoiceError(f"transcription took longer than {TRANSCRIBE_TIMEOUT}s")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise VoiceError(
            "transcription failed" + (f": {detail[-1]}" if detail else f" (exit code {result.returncode})")
        )
    text = clean_transcript(result.stdout)
    if not text:
        raise VoiceError("didn't catch anything; try again a little closer to the microphone")
    return text


def listen(
    record_command: str,
    transcribe_command: str,
    seconds: int,
    on_status: Callable[[str], None] | None = None,
) -> str:
    """Record from the microphone and return the transcript."""
    say = on_status or (lambda message: None)
    transcriber = resolve_transcriber(transcribe_command)
    recorder = record_command.strip() or find_recorder()
    if not recorder:
        raise VoiceError(
            "no audio recorder found. Install sox (`brew install sox`), arecord or ffmpeg, "
            "or set voice_record in forge.toml (FORGE_VOICE_RECORD)."
        )

    if not transcribe_command.strip() and not _builtin_model_cached():
        say("First use: downloading the speech model (about 150 MB, once)...")
        _prefetch_builtin_model()

    with tempfile.TemporaryDirectory(prefix="forge-voice-") as tmp:
        audio = os.path.join(tmp, "clip.wav")
        say(f"Listening (up to {seconds}s). Press Ctrl+C when you're done speaking...")
        record(recorder, audio, seconds)
        say("Transcribing...")
        return transcribe(transcriber, audio)
