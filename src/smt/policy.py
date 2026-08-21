"""Stable identity for the resolved trading policy."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass

from .config import StrategyConfig

POLICY_IDENTITY_VERSION = 2


@dataclass(frozen=True)
class TradingPolicyIdentity:
    fingerprint: str
    payload: dict


def trading_policy_identity(strategies: Iterable[StrategyConfig]) -> TradingPolicyIdentity:
    """Hash resolved strategy settings so trade evidence is attributable."""
    resolved = sorted(strategies, key=lambda strategy: strategy.name)
    payload = {
        "version": POLICY_IDENTITY_VERSION,
        "strategies": {
            strategy.name: strategy.model_dump(mode="json", exclude={"name"})
            for strategy in resolved
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return TradingPolicyIdentity(fingerprint=fingerprint, payload=payload)


def strategy_exit_snapshot(strategy: StrategyConfig) -> str:
    """Canonical JSON persisted on a trade at entry."""
    return json.dumps(strategy.exit.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
