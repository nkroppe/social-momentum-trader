"""Shared test helpers."""

from __future__ import annotations

from smt.config import _INHERITED_FIELDS as _INHERIT
from smt.config import MarketConfig, RiskConfig, StrategyConfig, UniverseConfig, get_market
from smt.store import Store


def social_only_market_cfg() -> MarketConfig:
    """Market config with price gates off, for tests that isolate social logic.

    Production always runs with these on; disabling them explicitly keeps the
    intent visible instead of relying on a missing market provider.
    """
    cfg = get_market().model_copy(deep=True)
    cfg.confirmation.enabled = False
    cfg.confirmation.fail_closed = False
    cfg.regime.enabled = False
    return cfg


def make_universe() -> UniverseConfig:
    """Two mid-tier symbols, so the default hybrid signal profile applies."""
    return UniverseConfig(
        quote_currency="USD",
        symbols={
            "SOL": {"product_id": "SOL-USD", "aliases": ["sol", "solana", "$sol"], "tier": "mid"},
            "BTC": {"product_id": "BTC-USD", "aliases": ["btc", "bitcoin"], "tier": "mid"},
        },
    )


def make_store(tmp_path) -> Store:
    s = Store(f"sqlite:///{tmp_path}/t.sqlite")
    s.init_db()
    return s


def make_strategy(name: str = "intraday", allocation: float = 0.5, **overrides) -> StrategyConfig:
    """Build a StrategyConfig from global RiskConfig defaults + overrides."""
    risk = RiskConfig()
    base = {f: getattr(risk, f) for f in _INHERIT}
    base.update(name=name, enabled=True, allocation=allocation)
    base.update(overrides)
    return StrategyConfig(**base)
