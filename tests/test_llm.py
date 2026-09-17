import json

import pytest

from forge import llm


class FakeClient:
    """Stands in for `ollama.Client`: `.chat()` returns the next canned
    response, or raises it if it's an exception instance.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, model, messages, tools):
        self.calls += 1
        if not self._responses:
            raise AssertionError("FakeClient ran out of canned responses")
        resp = self._responses.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        return resp


def assistant_response(content="", tool_calls=None, prompt_tokens=1, completion_tokens=1):
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "message": msg,
        "prompt_eval_count": prompt_tokens,
        "eval_count": completion_tokens,
    }


def tool_call(name, arguments):
    return {"function": {"name": name, "arguments": arguments}}


@pytest.fixture(autouse=True)
def _reset_usage():
    llm.reset_usage()
    yield
    llm.reset_usage()


# ---------------------------------------------------------------------------
# new_history
# ---------------------------------------------------------------------------


def test_new_history_is_empty_no_persona():
    assert llm.new_history() == []


# ---------------------------------------------------------------------------
# _parse_tool_arguments (bug #4)
# ---------------------------------------------------------------------------


def test_parse_tool_arguments_valid_json_string():
    parsed, error = llm._parse_tool_arguments(json.dumps({"path": "a.txt"}))
    assert parsed == {"path": "a.txt"}
    assert error is None


def test_parse_tool_arguments_already_a_dict():
    parsed, error = llm._parse_tool_arguments({"path": "a.txt"})
    assert parsed == {"path": "a.txt"}
    assert error is None


def test_parse_tool_arguments_none():
    parsed, error = llm._parse_tool_arguments(None)
    assert parsed == {}
    assert error is None


def test_parse_tool_arguments_empty_string():
    parsed, error = llm._parse_tool_arguments("")
    assert parsed == {}
    assert error is None


def test_parse_tool_arguments_malformed_json_does_not_raise():
    """Regression test for bug #4: a small local model can return
    truncated/malformed JSON for tool arguments. This must come back as
    an error, not raise JSONDecodeError.
    """
    parsed, error = llm._parse_tool_arguments('{"path": "a.txt"')  # truncated
    assert parsed is None
    assert error is not None
    assert "JSON" in error


def test_parse_tool_arguments_json_that_is_not_an_object():
    parsed, error = llm._parse_tool_arguments("[1, 2, 3]")
    assert parsed is None
    assert "object" in error


def test_parse_tool_arguments_unsupported_type():
    parsed, error = llm._parse_tool_arguments(12345)
    assert parsed is None
    assert error is not None


# ---------------------------------------------------------------------------
# _extract_tool_call
# ---------------------------------------------------------------------------


def test_extract_tool_call_well_formed():
    name, args, error = llm._extract_tool_call(tool_call("read_file", '{"path": "a.txt"}'))
    assert name == "read_file"
    assert args == '{"path": "a.txt"}'
    assert error is None


def test_extract_tool_call_missing_function_key():
    name, args, error = llm._extract_tool_call({"id": "1"})
    assert name is None
    assert error is not None
    assert "function" in error


def test_extract_tool_call_missing_name():
    name, args, error = llm._extract_tool_call({"function": {"arguments": "{}"}})
    assert name is None
    assert error is not None
    assert "name" in error


def test_extract_tool_call_not_a_dict():
    name, args, error = llm._extract_tool_call("not a dict")
    assert name is None
    assert error is not None


# ---------------------------------------------------------------------------
# run_task
# ---------------------------------------------------------------------------


def test_run_task_happy_path_no_tool_calls():
    client = FakeClient([assistant_response(content="all done")])
    result = llm.run_task("do a thing", llm.new_history(), client, model="test-model")

    assert result.content == "all done"
    assert result.iterations == 1
    assert result.history[0] == {"role": "user", "content": "do a thing"}
    assert result.history[-1]["role"] == "assistant"


def test_run_task_executes_tool_call_and_feeds_result_back(tmp_path):
    (tmp_path / "a.txt").write_text("file contents")

    client = FakeClient(
        [
            assistant_response(
                tool_calls=[tool_call("read_file", json.dumps({"path": "a.txt"}))]
            ),
            assistant_response(content="the file says: file contents"),
        ]
    )

    result = llm.run_task(
        "read a.txt", llm.new_history(), client, model="test-model", base_dir=str(tmp_path)
    )

    assert result.content == "the file says: file contents"
    assert result.iterations == 2
    tool_messages = [m for m in result.history if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0]["content"] == "file contents"
    assert tool_messages[0]["name"] == "read_file"


def test_run_task_raises_llm_error_when_message_key_missing():
    client = FakeClient([{"not_a_message_field": True}])
    with pytest.raises(llm.LLMError):
        llm.run_task("do a thing", llm.new_history(), client, model="test-model")


def test_run_task_raises_llm_error_when_response_is_none():
    client = FakeClient([None])
    with pytest.raises(llm.LLMError):
        llm.run_task("do a thing", llm.new_history(), client, model="test-model")


def test_run_task_malformed_tool_call_missing_function_fed_back_not_crashed():
    client = FakeClient(
        [
            assistant_response(tool_calls=[{"id": "no-function-here"}]),
            assistant_response(content="recovered"),
        ]
    )

    result = llm.run_task("do a thing", llm.new_history(), client, model="test-model")

    assert result.content == "recovered"
    tool_messages = [m for m in result.history if m["role"] == "tool"]
    assert len(tool_messages) == 1
    assert "Malformed tool call" in tool_messages[0]["content"]


def test_run_task_malformed_tool_call_missing_name_fed_back_not_crashed():
    client = FakeClient(
        [
            assistant_response(tool_calls=[{"function": {"arguments": "{}"}}]),
            assistant_response(content="recovered"),
        ]
    )

    result = llm.run_task("do a thing", llm.new_history(), client, model="test-model")

    assert result.content == "recovered"
    tool_messages = [m for m in result.history if m["role"] == "tool"]
    assert "function.name" in tool_messages[0]["content"]


def test_run_task_malformed_json_tool_arguments_fed_back_not_crashed():
    client = FakeClient(
        [
            assistant_response(tool_calls=[tool_call("read_file", '{"path": "a.txt"')]),
            assistant_response(content="recovered"),
        ]
    )

    result = llm.run_task("do a thing", llm.new_history(), client, model="test-model")

    assert result.content == "recovered"
    tool_messages = [m for m in result.history if m["role"] == "tool"]
    assert "JSON" in tool_messages[0]["content"]


def test_run_task_connection_error_mid_task_raises_llm_error_not_raw_exception():
    """Simulates Ollama being reachable for the first call and then
    dropping the connection on a later call.
    """
    client = FakeClient(
        [
            assistant_response(
                tool_calls=[tool_call("read_file", json.dumps({"path": "a.txt"}))]
            ),
            ConnectionError("connection reset by peer"),
        ]
    )

    with pytest.raises(llm.LLMError):
        llm.run_task("do a thing", llm.new_history(), client, model="test-model")


def test_run_task_stops_after_max_iterations_without_raising():
    responses = [
        assistant_response(tool_calls=[tool_call("read_file", json.dumps({"path": "a.txt"}))])
        for _ in range(5)
    ]
    client = FakeClient(responses)

    result = llm.run_task(
        "loop forever", llm.new_history(), client, model="test-model", max_iterations=3
    )

    assert result.iterations == 3
    assert "3 iterations" in result.content


# ---------------------------------------------------------------------------
# usage tracking
# ---------------------------------------------------------------------------


def test_usage_accumulates_across_calls():
    client = FakeClient(
        [assistant_response(content="done", prompt_tokens=10, completion_tokens=5)]
    )
    llm.run_task("task one", llm.new_history(), client, model="test-model")

    usage = llm.get_usage()
    assert usage["prompt_tokens"] == 10
    assert usage["completion_tokens"] == 5
    assert usage["calls"] == 1

    client2 = FakeClient(
        [assistant_response(content="done", prompt_tokens=3, completion_tokens=2)]
    )
    llm.run_task("task two", llm.new_history(), client2, model="test-model")

    usage = llm.get_usage()
    assert usage["prompt_tokens"] == 13
    assert usage["completion_tokens"] == 7
    assert usage["calls"] == 2


def test_reset_usage_zeroes_counters():
    client = FakeClient(
        [assistant_response(content="done", prompt_tokens=10, completion_tokens=5)]
    )
    llm.run_task("task", llm.new_history(), client, model="test-model")
    assert llm.get_usage()["calls"] == 1

    llm.reset_usage()
    assert llm.get_usage() == {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
