"""Artifact support on the chat page: the /api/artifact endpoint and its CSP,
and the chat-only artifact system prompt."""

import json
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from forge import chatui, llm, main


@pytest.fixture
def server():
    srv = chatui.ChatServer(lambda message: "ok", lambda: [], port=0)
    yield srv
    srv.stop()


def _post(srv, path, payload, token=True):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Forge-Token"] = srv.token
    request = urllib.request.Request(
        srv.url + path, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        return urllib.request.urlopen(request)
    except urllib.error.HTTPError as e:
        return e


def test_artifact_is_stored_and_served_with_sandbox_csp(server):
    page = "<html><script>1+1</script></html>"
    response = _post(server, "/api/artifact", {"content": page})
    assert response.status == 200
    url = json.loads(response.read())["url"]

    served = urllib.request.urlopen(server.url + url)
    assert served.read().decode() == page
    csp = served.headers["Content-Security-Policy"]
    assert "sandbox allow-scripts" in csp
    assert "allow-same-origin" not in csp
    assert "connect-src" not in csp and "default-src 'none'" in csp


def test_artifact_post_needs_the_token(server):
    response = _post(server, "/api/artifact", {"content": "<p>x</p>"}, token=False)
    assert response.code == 403


def test_artifact_post_rejects_empty_content(server):
    response = _post(server, "/api/artifact", {"content": "  "})
    assert response.code == 400


def test_unknown_artifact_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(server.url + "/artifact/nope")
    assert error.value.code == 404


def test_chat_page_may_frame_only_itself(server):
    csp = urllib.request.urlopen(server.url + "/").headers["Content-Security-Policy"]
    assert "frame-src 'self'" in csp
    assert "script-src 'self'" in csp


def test_chat_reply_sends_the_artifact_prompt_but_does_not_keep_it(monkeypatch):
    config = SimpleNamespace(
        model="m",
        fast_model="",
        working_dir=".",
        shell_timeout=1,
        max_iterations=1,
        usage_file=None,
        session_file="unused",
    )
    state = main.REPLState(config=config, client=None)
    state.history = [{"role": "user", "content": "earlier"}]
    seen = {}

    def fake_run_task(task, history, *args, **kwargs):
        seen["history"] = history
        return llm.TaskResult(
            content="reply",
            history=[*history, {"role": "user", "content": task}, {"role": "assistant", "content": "reply"}],
        )

    monkeypatch.setattr(main.llm, "run_task", fake_run_task)
    monkeypatch.setattr(main.session, "save", lambda *a, **k: None)
    monkeypatch.setattr(main, "_web_workspace", main.Workspace({"main": state}, "main"))

    assert main._chat_reply("hello") == "reply"
    assert seen["history"][0] == {"role": "system", "content": chatui.ARTIFACT_PROMPT}
    assert all(m["role"] != "system" for m in state.history)
    assert [m["content"] for m in state.history] == ["earlier", "hello", "reply"]
    assert llm.new_history() == []
