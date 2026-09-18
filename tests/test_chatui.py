import http.client
import json
import re

import pytest

from forge.chatui import MAX_BODY_BYTES, ChatServer


@pytest.fixture
def server():
    calls = []

    def reply(message):
        calls.append(message)
        if message == "boom":
            raise RuntimeError("model exploded")
        return f"echo: {message}"

    chat = ChatServer(reply, lambda: [{"role": "user", "content": "earlier"}], port=0)
    chat.calls = calls
    yield chat
    chat.stop()


def request(server, method, path, body=None, headers=None, host=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    all_headers = {"Host": host or f"localhost:{server.port}"}
    all_headers.update(headers or {})
    conn.request(method, path, body=body, headers=all_headers)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response, data


def post_chat(server, message, headers=None):
    all_headers = {"Content-Type": "application/json", "X-Forge-Token": server.token}
    all_headers.update(headers or {})
    return request(server, "POST", "/api/chat", json.dumps({"message": message}), all_headers)


def test_serves_page_with_token_and_csp(server):
    response, data = request(server, "GET", "/")
    assert response.status == 200
    assert server.token in data.decode()
    assert "default-src 'none'" in response.getheader("Content-Security-Policy")


def test_page_includes_earlier_messages(server):
    _, data = request(server, "GET", "/")
    assert re.search(r'<li class="msg user">.*?<pre>earlier</pre>', data.decode(), re.S)


def test_page_escapes_message_text():
    hostile = [{"role": "assistant", "content": "<script>alert(1)</script> & <b>x</b>"}]
    chat = ChatServer(lambda m: "", lambda: hostile, port=0)
    try:
        _, data = request(chat, "GET", "/")
    finally:
        chat.stop()
    page = data.decode()
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; &lt;b&gt;x&lt;/b&gt;" in page


def test_page_reflects_history_changes_between_loads():
    messages = []
    chat = ChatServer(lambda m: "", lambda: list(messages), port=0)
    try:
        assert "later" not in request(chat, "GET", "/")[1].decode()
        messages.append({"role": "user", "content": "later"})
        assert "later" in request(chat, "GET", "/")[1].decode()
    finally:
        chat.stop()


def test_serves_assets_and_404s_unknown_paths(server):
    assert request(server, "GET", "/chat.js")[0].status == 200
    assert request(server, "GET", "/chat.css")[0].status == 200
    assert request(server, "GET", "/nope")[0].status == 404


def test_chat_returns_reply(server):
    response, data = post_chat(server, "  hello  ")
    assert response.status == 200
    assert json.loads(data) == {"reply": "echo: hello"}
    assert server.calls == ["hello"]


def test_wrong_host_is_forbidden(server):
    response, _ = request(server, "GET", "/", host="evil.example.com")
    assert response.status == 403
    response, _ = request(
        server, "POST", "/api/chat", "{}",
        {"Content-Type": "application/json", "X-Forge-Token": server.token}, host="evil.example.com",
    )
    assert response.status == 403
    assert server.calls == []


def test_missing_or_wrong_token_is_forbidden(server):
    body = json.dumps({"message": "hi"})
    response, _ = request(server, "POST", "/api/chat", body, {"Content-Type": "application/json"})
    assert response.status == 403
    response, _ = post_chat(server, "hi", {"X-Forge-Token": "wrong"})
    assert response.status == 403
    assert server.calls == []


def test_cross_origin_post_is_forbidden(server):
    response, _ = post_chat(server, "hi", {"Origin": "http://evil.example.com"})
    assert response.status == 403
    assert server.calls == []


def test_same_origin_post_is_allowed(server):
    response, _ = post_chat(server, "hi", {"Origin": f"http://localhost:{server.port}"})
    assert response.status == 200


def test_non_json_content_type_is_rejected(server):
    response, _ = request(
        server, "POST", "/api/chat", "message=hi",
        {"Content-Type": "text/plain", "X-Forge-Token": server.token},
    )
    assert response.status == 415
    assert server.calls == []


@pytest.mark.parametrize("body", ["not json", "[]", json.dumps({"message": ""}), json.dumps({"message": 5})])
def test_bad_bodies_are_400(server, body):
    response, _ = request(
        server, "POST", "/api/chat", body,
        {"Content-Type": "application/json", "X-Forge-Token": server.token},
    )
    assert response.status == 400
    assert server.calls == []


def test_oversized_body_is_413(server):
    response, _ = request(
        server, "POST", "/api/chat", "x" * (MAX_BODY_BYTES + 1),
        {"Content-Type": "application/json", "X-Forge-Token": server.token},
    )
    assert response.status == 413
    assert server.calls == []


def test_reply_error_is_500_and_server_survives(server):
    response, data = post_chat(server, "boom")
    assert response.status == 500
    assert "model exploded" in json.loads(data)["error"]
    assert post_chat(server, "still alive")[0].status == 200


def test_unsupported_methods_are_405(server):
    assert request(server, "PUT", "/api/chat", "{}")[0].status == 405
    assert request(server, "DELETE", "/")[0].status == 405
