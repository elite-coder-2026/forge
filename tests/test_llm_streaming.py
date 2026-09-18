import pytest

from forge import llm


class StreamingClient:
    """Fake ollama client: yields the given chunks when stream=True, else
    returns a single full response dict."""

    def __init__(self, chunks, full=None, error=None):
        self.chunks = chunks
        self.full = full
        self.error = error
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        if kwargs.get("stream"):
            return iter(self.chunks)
        return self.full


@pytest.fixture(autouse=True)
def _reset_usage():
    llm.reset_usage()
    yield
    llm.reset_usage()


def _chunks():
    return [
        {"message": {"content": "Hel"}},
        {"message": {"content": "lo"}},
        {"message": {"content": ""}, "done": True, "prompt_eval_count": 7, "eval_count": 3},
    ]


def test_tokens_forwarded_in_order_and_joined():
    client = StreamingClient(_chunks())
    seen = []
    result = llm.run_task("hi", llm.new_history(), client, "m", on_token=seen.append)
    assert seen == ["Hel", "lo"]
    assert result.content == "Hello"
    assert result.iterations == 1


def test_stream_flag_passed_when_on_token_given():
    client = StreamingClient(_chunks())
    llm.run_task("hi", llm.new_history(), client, "m", on_token=lambda t: None)
    assert client.calls[0]["stream"] is True


def test_usage_recorded_from_final_chunk():
    client = StreamingClient(_chunks())
    llm.run_task("hi", llm.new_history(), client, "m", on_token=lambda t: None)
    usage = llm.get_usage()
    assert usage["prompt_tokens"] == 7
    assert usage["completion_tokens"] == 3
    assert usage["calls"] == 1


def test_no_on_token_uses_blocking_call():
    client = StreamingClient([], full={"message": {"content": "done"}})
    result = llm.run_task("hi", llm.new_history(), client, "m")
    assert "stream" not in client.calls[0]
    assert result.content == "done"


def test_tool_calls_collected_across_chunks(tmp_path):
    (tmp_path / "a.txt").write_text("contents")
    call = {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}

    class TwoStep:
        def __init__(self):
            self.n = 0

        def chat(self, **kwargs):
            self.n += 1
            if self.n == 1:
                return iter(
                    [
                        {"message": {"content": "reading "}},
                        {"message": {"content": "", "tool_calls": [call]}, "done": True},
                    ]
                )
            return iter([{"message": {"content": "finished"}, "done": True}])

    seen = []
    result = llm.run_task(
        "go", llm.new_history(), TwoStep(), "m", base_dir=str(tmp_path), on_token=seen.append
    )
    assert result.iterations == 2
    assert result.content == "finished"
    assert seen == ["reading ", "\n", "finished"]
    assert any(m["role"] == "tool" and "contents" in m["content"] for m in result.history)


def test_error_before_stream_raises_llm_error():
    client = StreamingClient([], error=ConnectionError("down"))
    with pytest.raises(llm.LLMError):
        llm.run_task("hi", llm.new_history(), client, "m", on_token=lambda t: None)


def test_error_mid_stream_raises_llm_error():
    def gen():
        yield {"message": {"content": "partial"}}
        raise ConnectionError("dropped")

    class Client:
        def chat(self, **kwargs):
            return gen()

    with pytest.raises(llm.LLMError):
        llm.run_task("hi", llm.new_history(), Client(), "m", on_token=lambda t: None)
