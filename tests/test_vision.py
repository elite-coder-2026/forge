import os

import httpx
import pytest

from forge import llm, main, vision
from forge.config import Config

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture(autouse=True)
def _reset_usage():
    llm.reset_usage()
    yield
    llm.reset_usage()


@pytest.fixture
def image(tmp_path):
    path = tmp_path / "mock.png"
    path.write_bytes(PNG)
    return str(path)


class VisionClient:
    """Fake ollama client: records calls; answers the vision model, or the
    coding model with a plain final message."""

    def __init__(self, description="A blue login form.", error=None):
        self.description = description
        self.error = error
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        content = self.description if "images" in kwargs["messages"][-1] else "done"
        response = {"message": {"content": content}, "prompt_eval_count": 10, "eval_count": 4}
        # The coding model is streamed (run_task passes on_token); vision isn't.
        return iter([response]) if kwargs.get("stream") else response


# --- resolve_image ----------------------------------------------------------


def test_resolve_image_accepts_valid_absolute_and_relative(tmp_path, image):
    assert vision.resolve_image(image) == image
    assert vision.resolve_image("mock.png", str(tmp_path)) == image


def test_resolve_image_expands_home(monkeypatch, tmp_path, image):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert vision.resolve_image("~/mock.png") == image


@pytest.mark.parametrize(
    "name, content, message",
    [
        ("missing.png", None, "not found"),
        ("notes.txt", b"hi", "Unsupported"),
        ("fake.png", b"this is not an image", "valid image"),
    ],
)
def test_resolve_image_rejects_bad_files(tmp_path, name, content, message):
    if content is not None:
        (tmp_path / name).write_bytes(content)
    with pytest.raises(vision.VisionError, match=message):
        vision.resolve_image(name, str(tmp_path))


def test_resolve_image_rejects_oversized(monkeypatch, image):
    monkeypatch.setattr(vision, "MAX_IMAGE_BYTES", 10)
    with pytest.raises(vision.VisionError, match="too large"):
        vision.resolve_image(image)


@pytest.mark.parametrize(
    "header",
    [b"\xff\xd8\xff\xe0" + b"0" * 8, b"GIF89a" + b"0" * 8, b"RIFF\x00\x00\x00\x00WEBP"],
)
def test_other_image_formats_pass_the_sniff(tmp_path, header):
    (tmp_path / "x.webp").write_bytes(header)
    assert vision.resolve_image("x.webp", str(tmp_path))


# --- describe_images / enrich_task ------------------------------------------


def test_describe_images_sends_bytes_and_task_to_vision_model_without_tools(image):
    client = VisionClient()
    out = vision.describe_images(client, "vis-model", [image], "build this page")
    (call,) = client.calls
    assert call["model"] == "vis-model"
    assert "tools" not in call
    message = call["messages"][0]
    assert message["images"] == [PNG]
    assert "build this page" in message["content"]
    assert out == "A blue login form."


def test_describe_images_records_usage(image, tmp_path):
    usage_file = str(tmp_path / "usage.json")
    vision.describe_images(VisionClient(), "m", [image], "t", usage_file)
    assert llm.get_usage()["calls"] == 1
    assert llm.get_persisted_usage(usage_file)["prompt_tokens"] == 10


def test_enrich_task_without_images_is_unchanged():
    assert vision.enrich_task("just text", [], VisionClient(), "m") == "just text"


def test_enrich_task_appends_description_and_names(image):
    out = vision.enrich_task("build it", [image], VisionClient(), "vis-model")
    assert out.startswith("build it")
    assert "mock.png" in out and "vis-model" in out and "A blue login form." in out


def test_missing_vision_model_gives_pull_hint(image):
    class NotFound(Exception):
        status_code = 404

    with pytest.raises(vision.VisionError, match="ollama pull vis-model"):
        vision.describe_images(VisionClient(error=NotFound("nope")), "vis-model", [image], "t")


def test_unreachable_server_raises_unreachable(image):
    for error in (ConnectionError("refused"), httpx.ConnectError("refused")):
        with pytest.raises(llm.OllamaUnreachableError):
            vision.describe_images(VisionClient(error=error), "m", [image], "t")


def test_other_vision_errors_and_empty_description(image):
    with pytest.raises(vision.VisionError, match="failed"):
        vision.describe_images(VisionClient(error=ValueError("bad")), "m", [image], "t")
    with pytest.raises(vision.VisionError, match="no description"):
        vision.describe_images(VisionClient(description="   "), "m", [image], "t")


def test_vision_error_is_an_llm_error():
    assert issubclass(vision.VisionError, llm.LLMError)


# --- config -----------------------------------------------------------------


def test_vision_model_config(monkeypatch, tmp_path):
    for name in list(os.environ):
        if name.startswith("FORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))
    assert Config().vision_model == "gemma3:12b"
    (tmp_path / "forge.toml").write_text('vision_model = "file-vision"\n')
    assert Config.from_env().vision_model == "file-vision"
    monkeypatch.setenv("FORGE_VISION_MODEL", "env-vision")
    assert Config.from_env().vision_model == "env-vision"


# --- CLI / REPL integration -------------------------------------------------


def _config(tmp_path):
    return Config(model="coder", vision_model="vis", working_dir=str(tmp_path), usage_file="")


def test_parse_args_collects_repeated_image_flags():
    args, task = main.parse_args(["--image", "a.png", "--image", "b.png", "build", "this"])
    assert args.image == ["a.png", "b.png"] and task == "build this"


def test_run_once_describes_image_then_runs_coder_with_enriched_task(tmp_path, image, capsys):
    client = VisionClient()
    code = main.run_once("build it", _config(tmp_path), client, images=[image])
    assert code == 0
    vision_call, coder_call = client.calls
    assert vision_call["model"] == "vis" and "tools" not in vision_call
    assert coder_call["model"] == "coder" and coder_call["tools"]
    user_message = coder_call["messages"][0]  # list is appended to after the call
    assert "images" not in user_message
    assert "A blue login form." in user_message["content"]
    assert "Analyzing 1 image(s) with vis" in capsys.readouterr().out


def test_run_once_without_images_never_calls_vision_model(tmp_path):
    client = VisionClient()
    main.run_once("plain", _config(tmp_path), client)
    assert [c["model"] for c in client.calls] == ["coder"]


def test_run_once_reports_vision_failure_and_skips_coder(tmp_path, image, capsys):
    class NotFound(Exception):
        status_code = 404

    client = VisionClient(error=NotFound("x"))
    code = main.run_once("build it", _config(tmp_path), client, images=[image])
    assert code == 1
    assert "ollama pull vis" in capsys.readouterr().err
    assert [c["model"] for c in client.calls] == ["vis"]


def test_main_rejects_bad_image_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FORGE_WORKING_DIR", str(tmp_path))
    assert main.main(["--image", "nope.png", "task"]) == 2
    assert "Image not found" in capsys.readouterr().err


def _state(tmp_path):
    return main.REPLState(config=_config(tmp_path), client=VisionClient())


def test_image_command_queues_lists_and_clears(tmp_path, image):
    state = _state(tmp_path)
    assert "No images attached" in main.handle_slash_command("/image", state)
    assert "1 image(s) attached" in main.handle_slash_command(f"/image {image}", state)
    assert "1 image(s) attached" in main.handle_slash_command(f"/image {image}", state)  # deduped
    assert image in main.handle_slash_command("/image", state)
    assert "cleared" in main.handle_slash_command("/image clear", state)
    assert state.pending_images == []


def test_image_command_handles_quoted_paths_with_spaces(tmp_path):
    (tmp_path / "my mock.png").write_bytes(PNG)
    state = _state(tmp_path)
    main.handle_slash_command('/image "my mock.png"', state)
    assert state.pending_images == [str(tmp_path / "my mock.png")]


def test_image_command_rejects_bad_input_without_queueing(tmp_path):
    state = _state(tmp_path)
    assert main.handle_slash_command("/image nope.png", state).startswith("Error:")
    assert main.handle_slash_command('/image "unterminated', state).startswith("Error:")
    assert state.pending_images == []


def _repl(monkeypatch, config, client, lines, **kwargs):
    inputs = iter(lines)

    def fake_input(*_):
        try:
            return next(inputs)
        except StopIteration:
            raise EOFError

    monkeypatch.setattr("builtins.input", fake_input)
    main.run_repl(config, client, **kwargs)


def test_repl_image_is_used_once_then_cleared(monkeypatch, tmp_path, image):
    client = VisionClient()
    _repl(monkeypatch, _config(tmp_path), client, [f"/image {image}", "build it", "another task"])
    assert [c["model"] for c in client.calls] == ["vis", "coder", "coder"]


def test_repl_failed_vision_keeps_image_queued_for_retry(monkeypatch, tmp_path, image, capsys):
    client = VisionClient(error=ConnectionError("down"))
    state_config = _config(tmp_path)
    _repl(monkeypatch, state_config, client, [f"/image {image}", "build it"])
    assert "Ollama isn't reachable" in capsys.readouterr().out


def test_repl_starts_with_images_from_the_command_line(monkeypatch, tmp_path, image):
    client = VisionClient()
    _repl(monkeypatch, _config(tmp_path), client, ["build it"], images=[image])
    assert [c["model"] for c in client.calls] == ["vis", "coder"]
