"""Inbound Telegram commands for remote kill / resume control.

Exact, case-sensitive commands from the configured chat only:

- ``KILL`` — trip the kill switch (flatten + block new entries)
- ``START`` — clear the kill switch and resume the trading loop

Uses long-poll-free ``getUpdates`` each main-loop tick and persists the
update offset so restarts neither re-fire old commands nor miss new ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings, TelegramControlConfig
from ..logging_setup import get_logger
from .alerts import Alerter
from .killswitch import KillSwitch

log = get_logger("smt.telegram_control")

KILL_COMMAND = "KILL"
START_COMMAND = "START"


@dataclass(frozen=True)
class TelegramCommand:
    command: str
    update_id: int
    chat_id: str
    message_id: int


class TelegramControl:
    """Poll Telegram for authorized KILL / START commands."""

    def __init__(
        self,
        settings: Settings,
        config: TelegramControlConfig,
        *,
        client: httpx.Client | None = None,
    ):
        self.settings = settings
        self.config = config
        self._client = client or httpx.Client(timeout=config.request_timeout_seconds)
        self._owns_client = client is None
        self.state_path = Path(config.state_file)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._offset = self._load_offset()
        self._bootstrapped = self._offset > 0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @property
    def enabled(self) -> bool:
        return bool(
            self.config.enabled
            and self.settings.telegram_bot_token
            and self.settings.telegram_chat_id
        )

    def _load_offset(self) -> int:
        if not self.state_path.exists():
            return 0
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return max(0, int(data.get("offset", 0)))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return 0

    def _save_offset(self, offset: int) -> None:
        pending = self.state_path.with_suffix(f"{self.state_path.suffix}.tmp")
        pending.write_text(
            json.dumps({"offset": offset}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        pending.replace(self.state_path)
        self._offset = offset

    def _api(self, method: str, **params: Any) -> dict[str, Any] | None:
        url = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/{method}"
        try:
            resp = self._client.get(url, params=params)
            if resp.status_code != 200:
                log.warning(
                    "telegram %s rejected (HTTP %s): %s",
                    method,
                    resp.status_code,
                    resp.text[:300],
                )
                return None
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 - control path must never crash the loop
            log.warning("telegram %s failed: %s", method, exc)
            return None
        if not payload.get("ok"):
            log.warning("telegram %s returned not-ok: %s", method, str(payload)[:300])
            return None
        return payload

    def _authorized_chat(self, chat_id: object) -> bool:
        return str(chat_id) == str(self.settings.telegram_chat_id)

    def _parse_command(self, update: dict[str, Any]) -> TelegramCommand | None:
        message = update.get("message") or update.get("channel_post")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or not self._authorized_chat(chat_id):
            if chat_id is not None:
                log.warning(
                    "ignored telegram command from unauthorized chat_id=%s",
                    chat_id,
                )
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return None
        command = text.strip()
        if command not in {KILL_COMMAND, START_COMMAND}:
            return None
        return TelegramCommand(
            command=command,
            update_id=int(update["update_id"]),
            chat_id=str(chat_id),
            message_id=int(message.get("message_id") or 0),
        )

    def poll_commands(self) -> list[TelegramCommand]:
        """Fetch new updates and return authorized KILL/START commands."""
        if not self.enabled:
            return []

        params: dict[str, Any] = {
            "timeout": 0,
            "allowed_updates": json.dumps(["message"]),
        }
        if self._offset > 0:
            params["offset"] = self._offset

        payload = self._api("getUpdates", **params)
        if payload is None:
            return []

        updates = payload.get("result") or []
        if not isinstance(updates, list) or not updates:
            return []

        latest = max(int(update["update_id"]) for update in updates) + 1
        # First successful poll with no stored offset only advances the cursor
        # so historical chat history cannot trip the kill switch on deploy.
        if not self._bootstrapped:
            self._save_offset(latest)
            self._bootstrapped = True
            log.info(
                "telegram control bootstrapped at offset=%d (ignored %d backlog update(s))",
                latest,
                len(updates),
            )
            return []

        commands: list[TelegramCommand] = []
        for update in updates:
            parsed = self._parse_command(update)
            if parsed is not None:
                commands.append(parsed)
        self._save_offset(latest)
        return commands

    def apply(
        self,
        commands: list[TelegramCommand],
        kill: KillSwitch,
        alerter: Alerter | None = None,
    ) -> list[str]:
        """Apply commands in update order and acknowledge each one."""
        applied: list[str] = []
        for command in commands:
            if command.command == KILL_COMMAND:
                kill.trip("telegram KILL command")
                subject = "Kill switch active"
                body = (
                    "Received Telegram KILL. Flattening all positions and "
                    "pausing new entries until START."
                )
                applied.append(KILL_COMMAND)
            elif command.command == START_COMMAND:
                kill.clear()
                subject = "Trading resumed"
                body = (
                    "Received Telegram START. Kill switch cleared; "
                    "entries and exit management will resume on the next loop."
                )
                applied.append(START_COMMAND)
            else:
                continue
            log.critical("telegram control: %s from chat %s", command.command, command.chat_id)
            if alerter is not None:
                alerter.notify(subject, body, critical=command.command == KILL_COMMAND)
        return applied

    def poll_and_apply(
        self,
        kill: KillSwitch,
        alerter: Alerter | None = None,
    ) -> list[str]:
        return self.apply(self.poll_commands(), kill, alerter)
