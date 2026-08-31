"""Policy-bound paper soak generations and live-readiness evidence."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger

log = get_logger("smt.soak")
MAX_PRIOR_GENERATIONS = 10
# Ingest on/off (X/Reddit pause) changes evidence collection, not entry/exit/risk.
# Keep the soak clock when those are the only section diffs.
NON_RESETTING_SECTIONS = frozenset({"sources"})


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass
class PriorSoakGeneration:
    generation: int
    started_at: datetime
    ended_at: datetime
    active_fingerprint: str
    manifest: dict[str, str]
    invalidation_reason: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PriorSoakGeneration:
        return cls(
            generation=int(data.get("generation", 0)),
            started_at=_utc_datetime(data["started_at"]),
            ended_at=_utc_datetime(data["ended_at"]),
            active_fingerprint=str(data.get("active_fingerprint", data.get("fingerprint", ""))),
            manifest={str(key): str(value) for key, value in (data.get("manifest") or {}).items()},
            invalidation_reason=str(data.get("invalidation_reason", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "active_fingerprint": self.active_fingerprint,
            "manifest": self.manifest,
            "invalidation_reason": self.invalidation_reason,
        }


@dataclass
class SoakState:
    started_at: datetime
    mode: str = "paper"
    active_fingerprint: str = ""
    manifest: dict[str, str] | None = None
    generation: int = 1
    invalidation_reason: str = "initial start"
    changed_sections: list[str] | None = None
    history: list[PriorSoakGeneration] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SoakState:
        fingerprint = str(data.get("active_fingerprint", data.get("fingerprint", "")))
        return cls(
            started_at=_utc_datetime(data["started_at"]),
            mode=str(data.get("mode", "paper")),
            active_fingerprint=fingerprint,
            manifest={str(key): str(value) for key, value in (data.get("manifest") or {}).items()},
            generation=int(data.get("generation", 1 if fingerprint else 0)),
            invalidation_reason=str(data.get("invalidation_reason", "")),
            changed_sections=[str(section) for section in (data.get("changed_sections") or [])],
            history=[PriorSoakGeneration.from_dict(item) for item in (data.get("history") or [])][
                -MAX_PRIOR_GENERATIONS:
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "mode": self.mode,
            "active_fingerprint": self.active_fingerprint,
            "manifest": self.manifest or {},
            "generation": self.generation,
            "invalidation_reason": self.invalidation_reason,
            "changed_sections": self.changed_sections or [],
            "history": [item.to_dict() for item in (self.history or [])],
        }


class SoakTracker:
    """Persist policy-bound soak evidence, invalidating stale generations."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> SoakState | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return SoakState.from_dict(data)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    def _save(self, state: SoakState) -> None:
        pending = self.path.with_suffix(f"{self.path.suffix}.tmp")
        pending.write_text(
            json.dumps(state.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        pending.replace(self.path)

    def current_state(self) -> SoakState | None:
        """Return persisted state without starting or invalidating a generation."""
        return self._load()

    @staticmethod
    def _changed_sections(
        previous: SoakState,
        manifest: dict[str, str],
    ) -> list[str]:
        old_manifest = previous.manifest or {}
        if not previous.active_fingerprint:
            return ["legacy_state"]
        changed = sorted(
            name
            for name in set(old_manifest) | set(manifest)
            if old_manifest.get(name) != manifest.get(name)
        )
        return changed or ["unknown"]

    def ensure_started(
        self,
        mode: str = "paper",
        *,
        fingerprint: str | None = None,
        manifest: dict[str, str] | None = None,
    ) -> SoakState:
        """Start once, or automatically invalidate stale PAPER evidence."""
        existing = self._load()
        if existing is not None:
            if fingerprint is None or existing.active_fingerprint == fingerprint:
                return existing
            next_manifest = dict(manifest or {})
            changed = self._changed_sections(existing, next_manifest)
            if set(changed) <= NON_RESETTING_SECTIONS:
                existing.active_fingerprint = fingerprint
                existing.manifest = next_manifest
                self._save(existing)
                log.info(
                    "Paper soak generation %d kept (identity updated in place; changed=%s)",
                    existing.generation,
                    ",".join(changed),
                )
                return existing
            reason = (
                "legacy state had no trading-policy fingerprint"
                if not existing.active_fingerprint
                else "trading-policy fingerprint changed"
            )
            return self.restart(
                mode,
                fingerprint=fingerprint,
                manifest=manifest,
                reason=reason,
            )
        state = SoakState(
            started_at=datetime.now(UTC),
            mode=mode,
            active_fingerprint=fingerprint or "",
            manifest=dict(manifest or {}),
            generation=1,
        )
        self._save(state)
        log.info(
            "Paper soak generation %d started at %s (policy=%s)",
            state.generation,
            state.started_at.isoformat(),
            state.active_fingerprint[:12] or "unbound",
        )
        return state

    def restart(
        self,
        mode: str = "paper",
        *,
        fingerprint: str | None = None,
        manifest: dict[str, str] | None = None,
        reason: str = "manual soak-reset",
    ) -> SoakState:
        """Reset to now, retaining a bounded audit trail of prior evidence."""
        existing = self._load()
        now = datetime.now(UTC)
        history = list(existing.history or []) if existing else []
        changed_sections: list[str] = []
        if existing is not None:
            next_manifest = dict(manifest if manifest is not None else existing.manifest or {})
            next_fingerprint = (
                fingerprint if fingerprint is not None else existing.active_fingerprint
            )
            if existing.active_fingerprint != next_fingerprint:
                changed_sections = self._changed_sections(existing, next_manifest)
            history.append(
                PriorSoakGeneration(
                    generation=existing.generation,
                    started_at=existing.started_at,
                    ended_at=now,
                    active_fingerprint=existing.active_fingerprint,
                    manifest=dict(existing.manifest or {}),
                    invalidation_reason=reason,
                )
            )
            generation = max(1, existing.generation + 1)
        else:
            generation = 1
            next_fingerprint = fingerprint or ""
            next_manifest = dict(manifest or {})
        state = SoakState(
            started_at=now,
            mode=mode,
            active_fingerprint=next_fingerprint,
            manifest=next_manifest,
            generation=generation,
            invalidation_reason=reason,
            changed_sections=changed_sections,
            history=history[-MAX_PRIOR_GENERATIONS:],
        )
        self._save(state)
        log.warning(
            "Paper soak generation %d reset at %s: %s (changed=%s)",
            state.generation,
            state.started_at.isoformat(),
            reason,
            ",".join(changed_sections) or "none",
        )
        return state

    def policy_matches(self, fingerprint: str) -> bool:
        state = self._load()
        return state is not None and state.active_fingerprint == fingerprint

    def days_elapsed(self, fingerprint: str | None = None) -> float:
        state = self._load()
        if state is None or (fingerprint is not None and state.active_fingerprint != fingerprint):
            return 0.0
        delta = datetime.now(UTC) - state.started_at
        return max(0.0, delta.total_seconds() / 86400.0)

    def meets_minimum(self, min_days: int, fingerprint: str | None = None) -> bool:
        if fingerprint is not None and not self.policy_matches(fingerprint):
            return False
        return self.days_elapsed(fingerprint) >= min_days

    def summary_line(
        self,
        min_days: int,
        fingerprint: str | None = None,
    ) -> str:
        state = self._load()
        days = self.days_elapsed(fingerprint)
        started = state.started_at.isoformat() if state else "not started"
        generation = state.generation if state else 0
        active = state.active_fingerprint if state else ""
        matches = fingerprint is None or active == fingerprint
        if not matches:
            ready = "NOT READY (trading-policy fingerprint mismatch)"
        elif self.meets_minimum(min_days, fingerprint):
            ready = "READY"
        else:
            ready = f"need {max(0, min_days - int(days))} more day(s)"
        reason = state.invalidation_reason if state else "not started"
        changed = ",".join(state.changed_sections or []) if state else ""
        detail = f" | reason: {reason}"
        if changed:
            detail += f" | changed: {changed}"
        return (
            f"Soak generation: {generation} | policy: {active[:12] or 'missing'} | "
            f"started: {started} | elapsed: {days:.1f}d / {min_days}d min | "
            f"{ready}{detail}"
        )
