"""Paper soak tracking: start time, elapsed days, readiness for live."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..logging_setup import get_logger

log = get_logger("smt.soak")


@dataclass
class SoakState:
    started_at: datetime
    mode: str = "paper"
    config_fingerprint: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> SoakState:
        started = datetime.fromisoformat(data["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return cls(
            started_at=started,
            mode=data.get("mode", "paper"),
            config_fingerprint=data.get("config_fingerprint", ""),
        )


class SoakTracker:
    """Persists when the paper soak began (first run)."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> SoakState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return SoakState.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def _save(self, state: SoakState) -> None:
        self.path.write_text(
            json.dumps(
                {
                    "started_at": state.started_at.isoformat(),
                    "mode": state.mode,
                    "config_fingerprint": state.config_fingerprint,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def ensure_started(self, mode: str = "paper", config_fingerprint: str = "") -> SoakState:
        existing = self._load()
        if existing is not None:
            if config_fingerprint and existing.config_fingerprint != config_fingerprint:
                log.warning(
                    "Trading policy changed (%s -> %s); resetting PAPER soak",
                    existing.config_fingerprint[:12] or "legacy",
                    config_fingerprint[:12],
                )
                return self.restart(mode, config_fingerprint)
            return existing
        state = SoakState(
            started_at=datetime.now(UTC),
            mode=mode,
            config_fingerprint=config_fingerprint,
        )
        self._save(state)
        log.info("Paper soak started at %s", state.started_at.isoformat())
        return state

    def restart(self, mode: str = "paper", config_fingerprint: str = "") -> SoakState:
        """Reset the clock to now.

        Used after a change to entry or exit logic: soak days accumulated under
        different rules do not evidence the system that would go live.
        """
        state = SoakState(
            started_at=datetime.now(UTC),
            mode=mode,
            config_fingerprint=config_fingerprint,
        )
        self._save(state)
        log.warning("Paper soak clock reset to %s", state.started_at.isoformat())
        return state

    def days_elapsed(self) -> float:
        state = self._load()
        if state is None:
            return 0.0
        delta = datetime.now(UTC) - state.started_at
        return max(0.0, delta.total_seconds() / 86400.0)

    def meets_minimum(self, min_days: int) -> bool:
        return self.days_elapsed() >= min_days

    def summary_line(self, min_days: int) -> str:
        days = self.days_elapsed()
        state = self._load()
        started = state.started_at.isoformat() if state else "not started"
        ready = (
            "READY"
            if self.meets_minimum(min_days)
            else f"need {min_days - int(days)} more day(s)"
        )
        return f"Soak started: {started} | elapsed: {days:.1f}d / {min_days}d min | {ready}"
