import httpx
import pytest

from forge import llm, main


class RaisingClient:
    def __init__(self, error):
        self.error = error

    def chat(self, **kwargs):
        raise self.error


@pytest.mark.parametrize(
    "error",
    [ConnectionError("refused"), httpx.ConnectError("refused"), httpx.ReadTimeout("slow")],
)
@pytest.mark.parametrize("stream", [False, True])
def test_connection_failures_raise_unreachable(error, stream):
    on_token = (lambda t: None) if stream else None
    with pytest.raises(llm.OllamaUnreachableError):
        llm.run_task("hi", llm.new_history(), RaisingClient(error), "m", on_token=on_token)


def test_connection_drop_mid_stream_raises_unreachable():
    def gen():
        yield {"message": {"content": "partial"}}
        raise httpx.ReadError("dropped")

    class Client:
        def chat(self, **kwargs):
            return gen()

    with pytest.raises(llm.OllamaUnreachableError):
        llm.run_task("hi", llm.new_history(), Client(), "m", on_token=lambda t: None)


def test_other_errors_are_llm_error_but_not_unreachable():
    with pytest.raises(llm.LLMError) as exc:
        llm.run_task("hi", llm.new_history(), RaisingClient(ValueError("bad")), "m")
    assert not isinstance(exc.value, llm.OllamaUnreachableError)


def test_run_once_prints_clear_message_with_host(capsys):
    cfg = main.Config.from_env()
    cfg.host = "http://example.test:11434"
    code = main.run_once("task", cfg, RaisingClient(ConnectionError("refused")))
    err = capsys.readouterr().err
    assert code == 1
    assert "Ollama isn't reachable at http://example.test:11434" in err
    assert "Traceback" not in err


def test_error_message_passes_through_other_llm_errors():
    cfg = main.Config.from_env()
    assert main._error_message(llm.LLMError("boom"), cfg) == "boom"
