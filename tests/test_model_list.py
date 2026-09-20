from types import SimpleNamespace

from forge import main
from forge.config import Config


def _state(client, model="qwen2.5-coder"):
    return main.REPLState(config=Config(model=model), client=client)


class _Client:
    def __init__(self, response=None, error=None):
        self._response, self._error = response, error

    def list(self):
        if self._error:
            raise self._error
        return self._response


def test_model_alone_lists_models_and_marks_the_current_one():
    response = {"models": [
        {"model": "llama3.2:3b", "size": 2_000_000_000},
        {"model": "qwen2.5-coder", "size": 4_700_000_000},
    ]}
    out = main.handle_slash_command("/model", _state(_Client(response)))
    assert "* qwen2.5-coder  (4.7 GB)" in out
    assert "  llama3.2:3b  (2.0 GB)" in out
    assert out.index("llama3.2:3b") < out.index("qwen2.5-coder")  # sorted by name


def test_model_list_reads_object_shaped_responses():
    response = SimpleNamespace(models=[SimpleNamespace(model="phi4", size=None)])
    out = main.handle_slash_command("/model", _state(_Client(response), model="other"))
    assert "phi4" in out and "GB" not in out


def test_model_list_when_the_server_has_none():
    assert "No models found" in main.handle_slash_command("/model", _state(_Client({"models": []})))


def test_model_list_when_the_server_is_unreachable():
    out = main.handle_slash_command("/model", _state(_Client(error=ConnectionError("refused"))))
    assert out.startswith("Could not list models: ConnectionError")


def test_model_with_a_name_still_switches():
    state = _state(_Client({"models": []}))
    assert "Model set to 'phi4'" in main.handle_slash_command("/model phi4", state)
    assert state.config.model == "phi4"
