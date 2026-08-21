"""Tests for inbound Telegram KILL / START control."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from smt.config import Settings, TelegramControlConfig
from smt.ops.killswitch import KillSwitch
from smt.ops.telegram_control import TelegramControl


class FakeTransport(httpx.MockTransport):
    def __init__(self, updates: list[dict]):
        self.updates = updates
        self.calls: list[tuple[str, dict]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.rsplit("/", 1)[-1]
            params = dict(request.url.params)
            self.calls.append((method, params))
            if method == "getUpdates":
                return httpx.Response(200, json={"ok": True, "result": self.updates})
            return httpx.Response(200, json={"ok": True, "result": True})

        super().__init__(handler)


def _control(tmp_path: Path, updates: list[dict], chat_id: str = "8053970855"):
    transport = FakeTransport(updates)
    client = httpx.Client(transport=transport)
    settings = Settings(
        telegram_bot_token="token",
        telegram_chat_id=chat_id,
        kill_file=str(tmp_path / "KILL"),
    )
    config = TelegramControlConfig(state_file=str(tmp_path / "telegram_control.json"))
    control = TelegramControl(settings, config, client=client)
    return control, KillSwitch(settings.kill_file)


def test_first_poll_bootstraps_without_applying_backlog(tmp_path):
    control, kill = _control(
        tmp_path,
        [
            {
                "update_id": 10,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 8053970855},
                    "text": "KILL",
                },
            }
        ],
    )

    applied = control.poll_and_apply(kill)

    assert applied == []
    assert not kill.is_active()
    assert json.loads((tmp_path / "telegram_control.json").read_text())["offset"] == 11


def test_kill_and_start_from_authorized_chat(tmp_path):
    control, kill = _control(
        tmp_path,
        [
            {
                "update_id": 2,
                "message": {
                    "message_id": 7,
                    "chat": {"id": 8053970855},
                    "text": "KILL",
                },
            }
        ],
    )
    control._save_offset(1)
    control._bootstrapped = True

    assert control.poll_and_apply(kill) == ["KILL"]
    assert kill.is_active()
    assert "telegram KILL command" in Path(kill.path).read_text(encoding="utf-8")

    control, kill = _control(
        tmp_path,
        [
            {
                "update_id": 3,
                "message": {
                    "message_id": 8,
                    "chat": {"id": 8053970855},
                    "text": "START",
                },
            }
        ],
    )
    control._save_offset(3)
    control._bootstrapped = True

    assert control.poll_and_apply(kill) == ["START"]
    assert not kill.is_active()


def test_whitespace_padded_exact_commands_are_accepted(tmp_path):
    control, kill = _control(
        tmp_path,
        [
            {
                "update_id": 4,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 8053970855},
                    "text": "  KILL  ",
                },
            }
        ],
    )
    control._save_offset(1)
    control._bootstrapped = True
    assert control.poll_and_apply(kill) == ["KILL"]
    assert kill.is_active()


@pytest.mark.parametrize(
    "text",
    ["kill", "Kill", "START please", "please START", "STOP", "KILL NOW"],
)
def test_non_exact_commands_are_ignored(tmp_path, text):
    control, kill = _control(
        tmp_path,
        [
            {
                "update_id": 5,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 8053970855},
                    "text": text,
                },
            }
        ],
    )
    control._save_offset(1)
    control._bootstrapped = True

    assert control.poll_and_apply(kill) == []
    assert not kill.is_active()


def test_unauthorized_chat_is_ignored(tmp_path):
    control, kill = _control(
        tmp_path,
        [
            {
                "update_id": 9,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 999},
                    "text": "KILL",
                },
            }
        ],
    )
    control._save_offset(1)
    control._bootstrapped = True

    assert control.poll_and_apply(kill) == []
    assert not kill.is_active()
