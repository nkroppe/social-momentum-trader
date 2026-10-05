"""Lock the production trading-policy fingerprint to the gen-8 identity."""

from __future__ import annotations

from smt.config import (
    REPO_ROOT,
    get_market,
    get_risk,
    get_signals,
    get_sources,
    get_strategies,
    get_universe,
)
from smt.llm.config import get_llm
from smt.policy import trading_policy_identity

GEN8_FINGERPRINT = "c95a0ad410f445808cc45aa657cbf4eb159292db5541504e8c2b01b25511228c"


def test_trading_policy_fingerprint_locked_to_gen8(monkeypatch):
    monkeypatch.delenv("SMT_CONFIG_DIR", raising=False)
    config_dir = REPO_ROOT / "config"
    monkeypatch.setattr("smt.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("smt.llm.config.CONFIG_DIR", config_dir)
    for loader in (
        get_market,
        get_risk,
        get_signals,
        get_sources,
        get_strategies,
        get_universe,
        get_llm,
    ):
        loader.cache_clear()

    fingerprint = trading_policy_identity().fingerprint
    assert fingerprint.startswith("c95a0ad410f4")
    assert fingerprint == GEN8_FINGERPRINT
