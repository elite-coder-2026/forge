"""Voice input: dictate a task instead of typing it.

Local and tool-agnostic. forge records a short clip with whatever recorder
is installed (sox, arecord or ffmpeg, or your own command), then hands the
file to a speech-to-text command you configure and reads its stdout. Nothing
is sent to any service by forge itself; what your transcriber does is up to
you (whisper.cpp keeps everything on your machine).

Both commands are templates: `{audio}` is the WAV path, and `{seconds}` (in
the recorder) is the maximum length.
"""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
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


def record(template: str, audio: str, seconds: int) -> None:
    if "{audio}" not in template:
        raise VoiceError("voice_record must contain {audio} (where the recording is written)")
    try:
        result = subprocess.run(
            _fill(template, audio, seconds),
            shell=True,
            capture_output=True,
            text=True,
            timeout=seconds + RECORD_GRACE,
        )
    except subprocess.TimeoutExpired:
        raise VoiceError("the recorder didn't stop in time")
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise VoiceError(
            "recording failed" + (f": {detail[-1]}" if detail else f" (exit code {result.returncode})")
            + ". Check microphone access for your terminal."
        )
    if not os.path.isfile(audio) or os.path.getsize(audio) == 0:
        raise VoiceError("nothing was recorded. Check microphone access for your terminal.")


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


def listen(record_command: str, transcribe_command: str, seconds: int) -> str:
    """Record from the microphone and return the transcript."""
    if not transcribe_command.strip():
        raise VoiceError(
            "voice input needs a speech-to-text command. Set voice_transcribe in forge.toml "
            "(or FORGE_VOICE_TRANSCRIBE), for example: "
            'voice_transcribe = "whisper-cli -m ~/models/ggml-base.en.bin -f {audio} -nt"'
        )
    recorder = record_command.strip() or find_recorder()
    if not recorder:
        raise VoiceError(
            "no audio recorder found. Install sox (`brew install sox`), arecord or ffmpeg, "
            "or set voice_record in forge.toml (FORGE_VOICE_RECORD)."
        )

    with tempfile.TemporaryDirectory(prefix="forge-voice-") as tmp:
        audio = os.path.join(tmp, "clip.wav")
        record(recorder, audio, seconds)
        return transcribe(transcribe_command, audio)
