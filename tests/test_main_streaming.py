from forge import main


def test_live_printer_streams_and_does_not_reprint_final(capsys):
    p = main._LivePrinter()
    p("Hel")
    p("lo")
    p.finish(fallback="Hello")
    assert capsys.readouterr().out == "Hello\n"


def test_live_printer_prints_fallback_when_nothing_streamed(capsys):
    p = main._LivePrinter()
    p.finish(fallback="answer")
    assert capsys.readouterr().out == "answer\n"


def test_run_once_passes_on_token_and_prints_live(monkeypatch, capsys):
    def fake_run_task(*args, on_token=None, **kwargs):
        on_token("Hi")
        on_token("!")
        return type("R", (), {"content": "Hi!", "history": []})()

    monkeypatch.setattr(main.llm, "run_task", fake_run_task)
    cfg = main.Config.from_env()
    assert main.run_once("task", cfg, client=None) == 0
    assert capsys.readouterr().out == "Hi!\n"
