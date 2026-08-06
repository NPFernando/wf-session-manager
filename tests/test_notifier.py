from workspace_session_manager.config import NotificationConfig
from workspace_session_manager.notifier import send_telegram


class FakeSender:
    def __init__(self, *, result: bool = True) -> None:
        self.result = result
        self.calls: list[tuple[str, str, str, str, float]] = []

    def __call__(
        self, api_base: str, bot_token: str, chat_id: str, text: str, timeout: float
    ) -> bool:
        self.calls.append((api_base, bot_token, chat_id, text, timeout))
        return self.result


def enabled_config(**overrides: object) -> NotificationConfig:
    defaults: dict[str, object] = {
        "telegram_enabled": True,
        "telegram_bot_token": "test-token",
        "telegram_chat_id": "12345",
    }
    defaults.update(overrides)
    return NotificationConfig(**defaults)


def test_send_telegram_calls_sender_when_enabled_and_configured() -> None:
    sender = FakeSender()
    result = send_telegram(enabled_config(), "hello", sender=sender)
    assert result is True
    assert sender.calls == [("https://api.telegram.org", "test-token", "12345", "hello", 5.0)]


def test_send_telegram_is_noop_when_disabled() -> None:
    sender = FakeSender()
    result = send_telegram(enabled_config(telegram_enabled=False), "hello", sender=sender)
    assert result is False
    assert sender.calls == []


def test_send_telegram_is_noop_when_bot_token_missing() -> None:
    sender = FakeSender()
    result = send_telegram(enabled_config(telegram_bot_token=""), "hello", sender=sender)
    assert result is False
    assert sender.calls == []


def test_send_telegram_is_noop_when_chat_id_missing() -> None:
    sender = FakeSender()
    result = send_telegram(enabled_config(telegram_chat_id=""), "hello", sender=sender)
    assert result is False
    assert sender.calls == []


def test_send_telegram_returns_false_when_sender_fails() -> None:
    sender = FakeSender(result=False)
    result = send_telegram(enabled_config(), "hello", sender=sender)
    assert result is False
    assert len(sender.calls) == 1


def test_send_telegram_uses_configured_api_base_and_timeout() -> None:
    sender = FakeSender()
    config = enabled_config(telegram_api_base="https://relay.example.com", subprocess_timeout=12.0)
    send_telegram(config, "hello", sender=sender)
    assert sender.calls == [("https://relay.example.com", "test-token", "12345", "hello", 12.0)]


def test_urllib_telegram_sender_rejects_unsafe_scheme() -> None:
    from workspace_session_manager.notifier import urllib_telegram_sender

    result = urllib_telegram_sender("ftp://evil.example.com", "token", "chat", "hi", 5.0)
    assert result is False


def test_urllib_telegram_sender_rejects_plaintext_http() -> None:
    from workspace_session_manager.notifier import urllib_telegram_sender

    result = urllib_telegram_sender("http://relay.example.com", "token", "chat", "hi", 5.0)
    assert result is False
