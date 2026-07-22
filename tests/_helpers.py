"""Shared test helpers."""

from __future__ import annotations

from smt.config import RiskConfig, StrategyConfig, UniverseConfig
from smt.store import Store

_INHERIT = (
    "take_profit_pct",
    "stop_loss_pct",
    "time_stop_hours",
    "signal_min_zscore",
    "signal_min_distinct_sources",
    "signal_min_mentions",
    "scorer_bucket_minutes",
    "scorer_lookback_buckets",
    "max_position_pct",
    "max_open_positions",
    "max_trades_per_day",
    "daily_loss_halt_pct",
    "weekly_loss_halt_pct",
    "cooldown_minutes_after_stop",
    "min_order_notional_usd",
    "assumed_fee_pct_per_side",
)


def make_universe() -> UniverseConfig:
    return UniverseConfig(
        quote_currency="USD",
        symbols={
            "SOL": {"product_id": "SOL-USD", "aliases": ["sol", "solana", "$sol"]},
            "BTC": {"product_id": "BTC-USD", "aliases": ["btc", "bitcoin"]},
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
