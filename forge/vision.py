"""Image-to-code support: turn a screenshot or mockup into text the coding
model can work from.

Vision-capable local models (e.g. Gemma 3) generally can't also do tool
calling, and the coding model usually can't see images. So this is a
two-stage hand-off: a vision model describes the image in detail, and that
description is added to the task for the normal tool-calling model. The
conversation history (and saved session) stays plain text.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from . import llm

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024

DESCRIBE_PROMPT = """\
You are the eyes for a coding agent that cannot see images. Describe the \
attached image so the agent can rebuild it as code without seeing it.

Cover, precisely:
- Overall layout and structure (regions, nesting, grid/flex direction, alignment, spacing)
- Every visible component, in reading order, with its exact text
- Colors (best-guess hex values), fonts, sizes, weights, borders, radii, shadows
- Icons, images and their approximate size and position
- Anything that looks interactive (buttons, inputs, links) and its apparent state

Do not write code. Do not add anything you cannot see.\
"""


class VisionError(llm.LLMError):
    """An image couldn't be used, or the vision model failed."""


def _looks_like_image(header: bytes) -> bool:
    return (
        header.startswith(b"\x89PNG\r\n\x1a\n")
        or header.startswith(b"\xff\xd8\xff")
        or header.startswith(b"GIF8")
        or (header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    )


def resolve_image(path: str, base_dir: str = ".") -> str:
    """Validate an image the *user* pointed at and return its absolute path.

    Unlike the model's file tools, this isn't sandboxed to the project: a
    screenshot usually lives on the Desktop. The user chose the file, the
    model didn't.
    """
    expanded = os.path.expanduser(path)
    full = expanded if os.path.isabs(expanded) else os.path.join(base_dir, expanded)
    full = os.path.abspath(full)

    if not os.path.isfile(full):
        raise VisionError(f"Image not found: {path!r}")
    if os.path.splitext(full)[1].lower() not in IMAGE_EXTENSIONS:
        raise VisionError(
            f"Unsupported image type for {path!r} (use {', '.join(sorted(IMAGE_EXTENSIONS))})"
        )
    try:
        size = os.path.getsize(full)
        with open(full, "rb") as f:
            header = f.read(12)
    except OSError as e:
        raise VisionError(f"Could not read image {path!r}: {e}") from e
    if size > MAX_IMAGE_BYTES:
        raise VisionError(
            f"Image {path!r} is too large ({size / 1_048_576:.1f} MB; max "
            f"{MAX_IMAGE_BYTES // 1_048_576} MB)"
        )
    if not _looks_like_image(header):
        raise VisionError(f"{path!r} doesn't look like a valid image file")
    return full


def describe_images(
    client: Any,
    model: str,
    paths: list[str],
    task: str,
    usage_file: str | None = None,
) -> str:
    """Ask the vision `model` to describe `paths`, keeping `task` in mind."""
    try:
        images = []
        for path in paths:
            with open(path, "rb") as f:
                images.append(f.read())
    except OSError as e:
        raise VisionError(f"Could not read image: {e}") from e

    prompt = f"{DESCRIBE_PROMPT}\n\nThe user's request, for context:\n{task}"
    try:
        response = client.chat(
            model=model, messages=[{"role": "user", "content": prompt, "images": images}]
        )
    except (ConnectionError, httpx.TransportError) as e:
        raise llm.OllamaUnreachableError(f"{type(e).__name__}: {e}") from e
    except Exception as e:  # noqa: BLE001 - reported to the user, never raised raw
        if getattr(e, "status_code", None) == 404:
            raise VisionError(
                f"Vision model {model!r} isn't available. Run `ollama pull {model}`, "
                f"or set FORGE_VISION_MODEL / vision_model to a vision model you have."
            ) from e
        raise VisionError(f"Vision model {model!r} failed: {type(e).__name__}: {e}") from e

    llm._record_usage(response, usage_file)

    description = ((response or {}).get("message") or {}).get("content") or ""
    if not description.strip():
        raise VisionError(f"Vision model {model!r} returned no description")
    return description.strip()


def enrich_task(
    task: str,
    paths: list[str],
    client: Any,
    model: str,
    usage_file: str | None = None,
) -> str:
    """`task` plus a description of the attached images (unchanged if none)."""
    if not paths:
        return task
    description = describe_images(client, model, paths, task, usage_file)
    names = ", ".join(os.path.basename(p) for p in paths)
    return (
        f"{task}\n\n"
        f"[Attached image(s): {names}. You cannot see them; this description "
        f"was produced by the vision model {model}:]\n{description}"
    )
