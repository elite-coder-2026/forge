"""Built-in offline speech-to-text, used by voice input when you haven't
configured your own `voice_transcribe` command.

    python -m forge.transcribe clip.wav        print the transcript
    python -m forge.transcribe --prefetch      download the model, then exit

Needs the optional `faster-whisper` package (`pip install "forge[voice]"`).
The model is chosen with FORGE_WHISPER_MODEL (default `base.en`, about
150 MB, downloaded once to the Hugging Face cache). Everything runs on
this machine; the only network use is that one-time model download.
"""

from __future__ import annotations

import os
import sys

from . import ui

DEFAULT_MODEL = "base.en"


def model_name() -> str:
    return os.environ.get("FORGE_WHISPER_MODEL", "").strip() or DEFAULT_MODEL


def _load(name: str):
    from faster_whisper import WhisperModel  # heavy import, so only when used

    return WhisperModel(name, device="cpu", compute_type="int8")


def transcribe_file(path: str, name: str | None = None) -> str:
    model = _load(name or model_name())
    segments, _info = model.transcribe(path, beam_size=1, vad_filter=True)
    return " ".join(segment.text.strip() for segment in segments).strip()


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv == ["--prefetch"]:
            _load(model_name())
            return 0
        if len(argv) != 1:
            ui.emit("usage: python -m forge.transcribe FILE | --prefetch", err=True)
            return 2
        ui.emit(transcribe_file(argv[0]))
        return 0
    except ImportError:
        ui.emit('faster-whisper is not installed: pip install "forge[voice]"', err=True)
        return 1
    except Exception as e:  # noqa: BLE001 - reported as one line for the caller
        ui.emit(f"{type(e).__name__}: {e}", err=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
