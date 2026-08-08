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

    @classmethod
    def from_dict(cls, data: dict) -> SoakState:
        started = datetime.fromisoformat(data["started_at"])
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return cls(started_at=started, mode=data.get("mode", "paper"))


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
                {"started_at": state.started_at.isoformat(), "mode": state.mode},
                indent=2,
            ),
            encoding="utf-8",
        )

    def ensure_started(self, mode: str = "paper") -> SoakState:
        existing = self._load()
        if existing is not None:
            return existing
        state = SoakState(started_at=datetime.now(UTC), mode=mode)
        self._save(state)
        log.info("Paper soak started at %s", state.started_at.isoformat())
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
