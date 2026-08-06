"""Best-effort Telegram delivery for session state-change alerts."""

from __future__ import annotations

import json
import urllib.request
from typing import Protocol

from workspace_session_manager.config import NotificationConfig

REQUEST_USER_AGENT = "workspace-session-manager/1.0"


class TelegramSender(Protocol):
    def __call__(
        self, api_base: str, bot_token: str, chat_id: str, text: str, timeout: float
    ) -> bool: ...


def urllib_telegram_sender(
    api_base: str, bot_token: str, chat_id: str, text: str, timeout: float
) -> bool:
    """POST a Telegram sendMessage request; return True only on a confirmed ok response."""
    if not api_base.startswith("https://"):
        return False
    url = f"{api_base.rstrip('/')}/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 -- scheme validated above
        url,
        data=payload,
        headers={"Content-Type": "application/json", "User-Agent": REQUEST_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    return bool(body.get("ok"))


def send_telegram(
    config: NotificationConfig,
    text: str,
    *,
    sender: TelegramSender = urllib_telegram_sender,
) -> bool:
    """Send a Telegram alert if notifications are enabled and configured.

    Returns False (never raises) when disabled, unconfigured, or on any
    delivery failure -- a failed notification must never affect the caller's
    own control flow (e.g. the attention-scan loop this feeds).
    """
    if not config.telegram_enabled or not config.telegram_bot_token or not config.telegram_chat_id:
        return False
    return sender(
        config.telegram_api_base,
        config.telegram_bot_token,
        config.telegram_chat_id,
        text,
        config.subprocess_timeout,
    )
