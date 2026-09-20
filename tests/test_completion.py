from prompt_toolkit.document import Document

from forge import main
from forge.ui.components.input_box import _slash_completer


def _complete(completer, text):
    return list(completer.get_completions(Document(text), None))


COMMANDS = {"/help": "Show all commands", "/model": "List or switch models", "/mode": "Set the mode"}


def test_command_names_complete_with_a_description():
    completions = _complete(_slash_completer(COMMANDS, {}), "/mo")
    assert [c.text for c in completions] == ["/model", "/mode"]
    assert completions[0].display_meta_text == "List or switch models"
    assert completions[0].start_position == -3


def test_a_bare_slash_offers_every_command():
    assert len(_complete(_slash_completer(COMMANDS, {}), "/")) == 3


def test_plain_text_gets_no_completions():
    assert _complete(_slash_completer(COMMANDS, {}), "hello /he") == []
    assert _complete(_slash_completer(COMMANDS, {}), "/help\n/mo") == []


def test_arguments_complete_after_a_command():
    completer = _slash_completer(COMMANDS, {"/mode": lambda: ["default", "auto", "plan"]})
    assert [c.text for c in _complete(completer, "/mode ")] == ["default", "auto", "plan"]
    assert [c.text for c in _complete(completer, "/mode a")] == ["auto"]
    assert _complete(completer, "/mode a")[0].start_position == -1


def test_a_command_without_an_argument_list_offers_nothing_after_it():
    assert _complete(_slash_completer(COMMANDS, {}), "/help ") == []


def test_a_failing_argument_provider_offers_nothing():
    def boom():
        raise RuntimeError("server down")

    assert _complete(_slash_completer(COMMANDS, {"/model": boom}), "/model ") == []


class _Client:
    def __init__(self, models):
        self.models, self.calls = models, 0

    def list(self):
        self.calls += 1
        return {"models": [{"model": m} for m in self.models]}


def test_model_names_are_cached_between_keystrokes(monkeypatch):
    main._model_names_cache.update(at=0.0, names=[])
    client = _Client(["b:1", "a:2"])
    assert main._model_names(client) == ["a:2", "b:1"]
    main._model_names(client)
    assert client.calls == 1


def test_model_names_are_empty_when_the_server_is_down():
    main._model_names_cache.update(at=0.0, names=[])

    class Down:
        def list(self):
            raise ConnectionError("refused")

    assert main._model_names(Down()) == []


def test_every_slash_command_has_a_description():
    assert all(main.SLASH_COMMANDS.values())
    assert "/mode" in main.SLASH_COMMANDS and "/model" in main.SLASH_COMMANDS
