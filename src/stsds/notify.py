"""Node 10: executive alert. Messages carry the transaction id and reason code only, never PII."""

from __future__ import annotations

import logging
from typing import Protocol

import httpx

log = logging.getLogger("stsds.notify")


class Notifier(Protocol):
    def alert(self, txn_id: str, code: str, message: str, route: str) -> None: ...


class NullNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []

    def alert(self, txn_id: str, code: str, message: str, route: str) -> None:
        self.sent.append((txn_id, code, message, route))


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token, self._chat = bot_token, chat_id

    def alert(self, txn_id: str, code: str, message: str, route: str) -> None:
        text = f"STSDS {route}\ntxn {txn_id}\n{code}: {message}"[:3500]
        try:
            httpx.post(
                f"https://api.telegram.org/bot{self._token}/sendMessage",
                json={"chat_id": self._chat, "text": text},
                timeout=10.0,
            ).raise_for_status()
        except httpx.HTTPError as exc:  # alert failure must never mask the halt itself
            log.error("alert delivery failed for %s: %s", txn_id, type(exc).__name__)
