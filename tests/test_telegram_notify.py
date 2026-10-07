"""Unit tests for scripts/telegram_notify.py's message formatting and recipient handling."""

import pytest
import requests

import telegram_notify


def test_format_message_bolds_field_labels_only():
    text = telegram_notify.format_message("Заголовок", {"Назва": "Тестова монета", "Дата": "01 Січня 2026"})

    assert text == "Заголовок\n\n<b>Назва:</b> Тестова монета\n<b>Дата:</b> 01 Січня 2026"


def test_format_message_escapes_ampersand_and_angle_brackets_in_values():
    text = telegram_notify.format_message("Заголовок", {"Назва": 'Монета "A & B" <ok>'})

    assert '<b>Назва:</b> Монета "A &amp; B" &lt;ok&gt;' in text
    # The literal value must not leave a stray unescaped tag in the message.
    assert "<ok>" not in text


def test_format_message_leaves_quotes_and_apostrophes_unescaped():
    text = telegram_notify.format_message("Заголовок", {"Назва": "Пам'ятна монета \"Приклад\""})

    assert '<b>Назва:</b> Пам\'ятна монета "Приклад"' in text


def test_format_message_escapes_header_too():
    text = telegram_notify.format_message("A & B", {})

    assert text.splitlines()[0] == "A &amp; B"


def test_get_recipients_parses_multiple_pairs_split_on_last_colon(monkeypatch):
    # Bot tokens contain a colon themselves (id:hash), so splitting must
    # use the *last* colon in each entry, not the first.
    monkeypatch.setenv("TELEGRAM_RECIPIENTS", "111:AAAtoken:222\n333:BBBtoken:-444")

    assert telegram_notify.get_recipients() == [("111:AAAtoken", "222"), ("333:BBBtoken", "-444")]


def test_get_recipients_accepts_comma_separated_pairs_too(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RECIPIENTS", "111:AAAtoken:222, 333:BBBtoken:444")

    assert telegram_notify.get_recipients() == [("111:AAAtoken", "222"), ("333:BBBtoken", "444")]


def test_get_recipients_falls_back_to_single_bot_token_and_chat_id(monkeypatch):
    monkeypatch.delenv("TELEGRAM_RECIPIENTS", raising=False)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:AAAtoken")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "222")

    assert telegram_notify.get_recipients() == [("111:AAAtoken", "222")]


def test_get_recipients_raises_on_entry_without_colon(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RECIPIENTS", "not-a-valid-entry")

    with pytest.raises(ValueError):
        telegram_notify.get_recipients()


def test_send_telegram_sends_to_every_recipient(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RECIPIENTS", "token1:111\ntoken2:222")
    calls = []

    class FakeResponse:
        status_code = 200

        def raise_for_status(self):
            pass

    def fake_post(url, data, timeout):
        calls.append((url, data["chat_id"]))
        return FakeResponse()

    monkeypatch.setattr(telegram_notify.requests, "post", fake_post)

    telegram_notify.send_telegram("hello")

    assert calls == [
        ("https://api.telegram.org/bottoken1/sendMessage", "111"),
        ("https://api.telegram.org/bottoken2/sendMessage", "222"),
    ]


def test_send_telegram_retries_after_rate_limit_then_succeeds(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RECIPIENTS", "token1:111")
    statuses = [429, 200]
    slept = []

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {}

        def json(self):
            return {"parameters": {"retry_after": 7}}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(f"HTTP {self.status_code}")

    monkeypatch.setattr(
        telegram_notify.requests, "post", lambda *a, **kw: FakeResponse(statuses.pop(0))
    )

    telegram_notify.send_telegram("hello", sleep=slept.append)

    # Waited exactly as long as Telegram asked, then delivered.
    assert slept == [7.0]
    assert statuses == []


def test_retry_after_seconds_prefers_body_then_header_then_default():
    class Resp:
        def __init__(self, body, headers):
            self._body = body
            self.headers = headers

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    assert telegram_notify._retry_after_seconds(Resp({"parameters": {"retry_after": 5}}, {})) == 5.0
    assert telegram_notify._retry_after_seconds(Resp({}, {"Retry-After": "9"})) == 9.0
    assert telegram_notify._retry_after_seconds(Resp(None, {})) == telegram_notify.DEFAULT_RETRY_AFTER_SECONDS


def test_send_telegram_tries_every_recipient_even_if_one_fails(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RECIPIENTS", "badtoken:111,goodtoken:222")
    attempted = []

    class FakeResponse:
        def __init__(self, chat_id):
            self.chat_id = chat_id
            # 403 stands in for "bot was kicked from this chat" - a real
            # failure that no amount of retrying will fix.
            self.status_code = 403 if chat_id == "111" else 200

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError("boom")

    def fake_post(url, data, timeout):
        attempted.append(data["chat_id"])
        return FakeResponse(data["chat_id"])

    monkeypatch.setattr(telegram_notify.requests, "post", fake_post)

    with pytest.raises(RuntimeError):
        telegram_notify.send_telegram("hello")

    # Both recipients were attempted despite the first one failing.
    assert attempted == ["111", "222"]


@pytest.mark.parametrize("outcome", [200, 401, 429, 503, "timeout", "connection"])
def test_delivery_outcomes_are_safe_and_continue(monkeypatch, capsys, outcome):
    import traceback

    token, chat_id = "123:synthetic-private-token", "-987654321"
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    monkeypatch.setenv("TELEGRAM_RECIPIENTS", f"{token}:{chat_id},other-token:second-chat")
    attempts, waits = [], []

    def post(request_url, data, timeout):
        attempts.append(data["chat_id"])
        assert timeout == 30
        if data["chat_id"] == chat_id and isinstance(outcome, str):
            error = requests.Timeout if outcome == "timeout" else requests.ConnectionError
            raise error(f"{url} chat_id={chat_id}")
        response = requests.Response()
        response.status_code = outcome if data["chat_id"] == chat_id else 200
        response.url = request_url
        response._content = b'{"parameters": {"retry_after": 0}}'
        return response

    monkeypatch.setattr(telegram_notify.requests, "post", post)
    diagnostic = ""
    if outcome == 200:
        telegram_notify.send_telegram("hello", sleep=waits.append)
    else:
        with pytest.raises(RuntimeError) as caught:
            telegram_notify.send_telegram("hello", sleep=waits.append)
        diagnostic = "".join(traceback.format_exception(caught.value))
        assert ("timeout" if outcome == "timeout" else "connection" if outcome == "connection" else f"HTTP {outcome}") in diagnostic
    output = capsys.readouterr()
    diagnostic += output.out + output.err
    for private in (token, chat_id, url, "api.telegram.org", "other-token", "second-chat"):
        assert private not in diagnostic
    assert attempts == [chat_id] * (3 if outcome == 429 else 1) + ["second-chat"]
    assert waits == ([0.0, 0.0] if outcome == 429 else [])


def test_invalid_recipient_diagnostic_does_not_echo_input(monkeypatch):
    monkeypatch.setenv("TELEGRAM_RECIPIENTS", "synthetic-private-token")
    with pytest.raises(ValueError) as caught:
        telegram_notify.get_recipients()
    assert "synthetic-private-token" not in str(caught.value)
