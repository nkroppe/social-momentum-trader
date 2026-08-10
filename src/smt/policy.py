"""Canonical identity for the complete policy that can affect trading."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .config import (
    MarketConfig,
    RiskConfig,
    SignalsConfig,
    SourcesConfig,
    StrategiesConfig,
    UniverseConfig,
    get_market,
    get_risk,
    get_signals,
    get_sources,
    get_strategies,
    get_universe,
)
from .llm.config import LLMConfig, get_llm

TRADING_POLICY_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class TradingPolicyIdentity:
    """Full SHA-256 identity plus short, non-sensitive section diagnostics."""

    fingerprint: str
    manifest: dict[str, str]


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _source_policy(config: SourcesConfig) -> dict[str, Any]:
    """Collection and weighting rules affect the evidence seen by strategies."""
    return config.model_dump(mode="json")


def _llm_policy(config: LLMConfig) -> dict[str, Any]:
    """Keep decision policy, but omit local state paths and advisory reflection."""
    judge = config.judge.model_dump(
        mode="json",
        exclude={"state_file"},
    )
    return {
        "enabled": config.enabled,
        "provider": config.provider,
        "model_family": config.model_family,
        "max_calls_per_month": config.max_calls_per_month,
        "judge": judge,
    }


def trading_policy_identity(
    *,
    strategies: StrategiesConfig | None = None,
    risk: RiskConfig | None = None,
    market: MarketConfig | None = None,
    signals: SignalsConfig | None = None,
    universe: UniverseConfig | None = None,
    sources: SourcesConfig | None = None,
    llm: LLMConfig | None = None,
) -> TradingPolicyIdentity:
    """Hash resolved trading policy, excluding secrets and operational state.

    Pydantic's resolved models make omitted YAML defaults explicit. The schema
    version deliberately invalidates prior evidence if the canonical contract
    itself changes.
    """
    sections: dict[str, Any] = {
        "schema": {"version": TRADING_POLICY_SCHEMA_VERSION},
        "strategies": (strategies or get_strategies()).model_dump(mode="json"),
        "risk": (risk or get_risk()).model_dump(mode="json"),
        "market": (market or get_market()).model_dump(mode="json"),
        "signals": (signals or get_signals()).model_dump(mode="json"),
        "universe": (universe or get_universe()).model_dump(mode="json"),
        "sources": _source_policy(sources or get_sources()),
        "llm": _llm_policy(llm or get_llm()),
    }
    manifest = {
        name: hashlib.sha256(_canonical_bytes(value)).hexdigest()[:12]
        for name, value in sections.items()
    }
    fingerprint = hashlib.sha256(_canonical_bytes(sections)).hexdigest()
    return TradingPolicyIdentity(fingerprint=fingerprint, manifest=manifest)
