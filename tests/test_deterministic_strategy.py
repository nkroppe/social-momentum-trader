"""Regression tests for deterministic entries, risk sizing, and PAPER exits."""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from _helpers import make_store, make_strategy, make_universe
from sqlalchemy import inspect

from smt.config import (
    EntryRulesConfig,
    MarketConfig,
    RiskConfig,
    Settings,
    SignalsConfig,
    TierConfig,
    UniverseConfig,
    get_strategies,
)
from smt.market import (
    Candle,
    TechnicalSnapshot,
    TopOfBookQuote,
    aggregate_candles,
    ema,
    relative_volume,
    rolling_vwap,
    rsi,
    structure_levels,
    volatility_compression,
)
from smt.models import ExitReason, SocialEvent, Trade, TradeStatus, utcnow
from smt.ops.preflight import run_preflight
from smt.scorer import ScoreResult
from smt.store import Store
from smt.trader.broker import Fill
from smt.trader.execution import ExecutionCostEstimator
from smt.trader.manager import TradeManager
from smt.trader.paper import PaperBroker
from smt.trader.risk import RiskGate
from smt.trader.signals import SignalEngine, TradeCandidate, detect_price_setup


def _candles(count: int = 70, *, step: float = 0.2, volume: float = 100.0) -> list[Candle]:
    rows: list[Candle] = []
    for i in range(count):
        close = 100.0 + i * step
        rows.append(
            Candle(
                ts=i * 900,
                open=close - 0.05,
                high=close + 0.10,
                low=close - 0.10,
                close=close,
                volume=volume,
            )
        )
    return rows


def _down_candles(count: int = 70, *, step: float = 0.2, volume: float = 100.0) -> list[Candle]:
    rows: list[Candle] = []
    for i in range(count):
        close = 100.0 + (count - 1 - i) * step
        rows.append(
            Candle(
                ts=i * 900,
                open=close + 0.05,
                high=close + 0.10,
                low=close - 0.10,
                close=close,
                volume=volume,
            )
        )
    return rows


def _direct_breakout() -> list[Candle]:
    rows = _candles()
    prior_high = max(c.high for c in rows[-21:-1])
    rows[-1] = Candle(
        ts=rows[-1].ts,
        open=prior_high - 0.1,
        high=prior_high + 1.2,
        low=prior_high - 0.2,
        close=prior_high + 1.0,
        volume=200.0,
    )
    return rows


def _retest() -> list[Candle]:
    rows = _candles()
    idx = len(rows) - 2
    level = max(c.high for c in rows[idx - 20 : idx])
    rows[idx] = Candle(
        ts=rows[idx].ts,
        open=level - 0.1,
        high=level + 1.2,
        low=level - 0.2,
        close=level + 1.0,
        volume=300.0,
    )
    rows[-1] = Candle(
        ts=rows[-1].ts,
        open=level + 0.1,
        high=level + 0.5,
        low=level - 0.1,
        close=level + 0.3,
        volume=100.0,
    )
    return rows


def test_price_indicators_and_structure_are_deterministic():
    rows = _candles()
    assert ema(rows, 9) > ema(rows, 21) > ema(rows, 50)
    assert rsi(rows, 14) == 100.0
    assert rolling_vwap(rows, 20) > 0
    assert relative_volume(_direct_breakout(), 20) == pytest.approx(2.0)
    high, low = structure_levels(rows, 20)
    assert high == max(c.high for c in rows[-21:-1])
    assert low == min(c.low for c in rows[-21:-1])


def test_volatility_compression_compares_recent_to_prior_range():
    rows: list[Candle] = []
    for i in range(21):
        spread = 0.25 if 15 <= i < 20 else 2.0
        rows.append(Candle(i, 100 - spread, 100 + spread, 100, 100, 100))
    assert volatility_compression(rows, lookback=20, recent=5, max_ratio=0.5)


def test_four_hour_aggregation_is_utc_aligned_ohlcv():
    hourly = [
        Candle(ts=i * 3600, open=100 + i, high=102 + i, low=99 + i, close=101 + i, volume=10)
        for i in range(8)
    ]
    bars = aggregate_candles(hourly, 14_400)
    assert [bar.ts for bar in bars] == [0, 14_400]
    assert bars[0].open == 100
    assert bars[0].close == 104
    assert bars[0].high == 105
    assert bars[0].low == 99
    assert bars[0].volume == 40


def test_direct_breakout_and_required_retest_are_separate_setups():
    rules = EntryRulesConfig(require_compression=False, allow_vwap_pullback=False)
    preferred = TierConfig(
        social_policy="ignored", min_relative_volume=1.5, retest_policy="preferred"
    )
    required = preferred.model_copy(update={"retest_policy": "required"})
    bias = _candles()

    direct = detect_price_setup(_direct_breakout(), bias, rules, preferred, "intraday")
    assert direct is not None
    assert direct.name == "breakout_close"
    assert direct.structure_stop < direct.entry_price
    assert 0 < direct.stop_pct <= rules.max_stop_pct
    assert detect_price_setup(_direct_breakout(), bias, rules, required, "intraday") is None

    retest = detect_price_setup(_retest(), bias, rules, required, "intraday")
    assert retest is not None
    assert retest.name == "breakout_retest"
    assert retest.metadata["relative_volume"] == pytest.approx(3.0)


def test_disabled_retest_skips_retest_but_keeps_close_breakouts():
    rules = EntryRulesConfig(
        require_compression=False,
        allow_vwap_pullback=False,
        allow_breakout_retest=False,
        min_stop_pct=0.02,
    )
    preferred = TierConfig(
        social_policy="ignored", min_relative_volume=1.5, retest_policy="preferred"
    )
    required = preferred.model_copy(update={"retest_policy": "required"})
    bias = _candles()

    direct = detect_price_setup(_direct_breakout(), bias, rules, preferred, "intraday")
    assert direct is not None
    assert direct.name == "breakout_close"
    assert direct.stop_pct >= 0.02
    assert detect_price_setup(_retest(), bias, rules, preferred, "intraday") is None
    assert detect_price_setup(_retest(), bias, rules, required, "intraday") is None
    assert detect_price_setup(_direct_breakout(), bias, rules, required, "intraday") is None


def test_swing_bias_rejects_bearish_stack_without_requiring_bullish_stack():
    rules = EntryRulesConfig(
        require_compression=False,
        allow_vwap_pullback=False,
        require_bias_ema_stack=False,
        reject_bearish_bias_stack=True,
        rsi_min=50,
    )
    preferred = TierConfig(
        social_policy="ignored", min_relative_volume=1.5, retest_policy="preferred"
    )
    trigger = _direct_breakout()
    assert detect_price_setup(trigger, _candles(), rules, preferred, "swing") is not None
    assert detect_price_setup(trigger, _down_candles(), rules, preferred, "swing") is None


def test_intraday_and_swing_rules_are_structurally_distinct():
    strategies = {strategy.name: strategy for strategy in get_strategies().enabled()}
    assert strategies["intraday"].entry.trigger_granularity_seconds == 900
    assert strategies["intraday"].entry.bias_granularity_seconds == 3_600
    assert strategies["intraday"].entry.allow_vwap_pullback
    assert not strategies["intraday"].entry.allow_breakout_retest
    assert strategies["intraday"].entry.min_stop_pct == 0.02
    assert strategies["intraday"].atr_min_stop_pct == 0.02
    assert not strategies["intraday"].entry.require_compression
    assert strategies["swing"].entry.trigger_granularity_seconds == 3_600
    assert strategies["swing"].entry.bias_granularity_seconds == 14_400
    # Ledger-backed: compression is no longer a hard gate. Gen-5 grind: RSI 50
    # and refuse a bearish 4h stack instead of requiring a full bullish stack.
    assert not strategies["swing"].entry.require_compression
    assert not strategies["swing"].entry.allow_vwap_pullback
    assert strategies["swing"].entry.rsi_min == 50
    assert not strategies["swing"].entry.require_bias_ema_stack
    assert strategies["swing"].entry.reject_bearish_bias_stack
    assert strategies["bear_rally"].regime_mode == "risk_off_only"
    assert strategies["bear_rally"].entry.setup_family == "bear_rally"


class _SetupMarket:
    def __init__(
        self,
        snapshot: TechnicalSnapshot | None = None,
        *,
        setup_data_available: bool = True,
    ):
        self._snapshot = snapshot
        self.setup_data_available = setup_data_available

    def candles(self, _product: str, granularity: int | None = None) -> list[Candle]:
        if not self.setup_data_available:
            return []
        return _direct_breakout() if granularity == 900 else _candles()

    def snapshot(self, product: str, sma_periods: int, lookback_periods: int) -> TechnicalSnapshot:
        del sma_periods, lookback_periods
        return self._snapshot or TechnicalSnapshot(
            product_id=product,
            ok=True,
            price=120,
            sma=100,
            trailing_return=0.03,
            volume_z=2.0,
        )

    def regime_ok(self) -> tuple[bool, str]:
        return True, "test"


def _score(ticker: str, *, bullish: float = 0.9) -> ScoreResult:
    return ScoreResult(
        ticker=ticker,
        zscore=6.0,
        recent=30.0,
        baseline_mean=2.0,
        mentions_window=40,
        distinct_sources=2,
        distinct_authors=12,
        bullish_ratio=bullish,
        directional_posts=20,
        baseline_kind="trailing",
        reason="test",
    )


def test_tier_playbooks_keep_price_hard_and_apply_social_afterward():
    universe = UniverseConfig(
        symbols={
            "BTC": {"product_id": "BTC-USD", "tier": "major"},
            "SOL": {"product_id": "SOL-USD", "tier": "large"},
        }
    )
    signals = SignalsConfig(
        social_decision_mode="enforce",
        tiers={
            "major": TierConfig(
                social_policy="ignored",
                min_relative_volume=1.5,
                retest_policy="preferred",
            ),
            "large": TierConfig(
                social_policy="optional",
                min_relative_volume=1.5,
                retest_policy="preferred",
                optional_social_boost=1.1,
            ),
        },
    )
    engine = SignalEngine(
        make_strategy(),
        universe,
        signals,
        _SetupMarket(),  # type: ignore[arg-type]
        MarketConfig(),
    )

    major = engine.candidates([_score("BTC", bullish=0.0)])
    assert len(major) == 1  # majors are price-only
    large = engine.candidates([_score("SOL")])
    assert large[0].conviction == pytest.approx(0.85 * 1.1)
    assert engine.candidates([_score("SOL", bullish=0.0)]) == []  # optional-social veto


@pytest.mark.parametrize(
    "snapshot",
    [
        TechnicalSnapshot("BTC-USD", True, price=90, sma=100, trailing_return=0.03, volume_z=2),
        TechnicalSnapshot("BTC-USD", True, price=120, sma=100, trailing_return=-0.01, volume_z=2),
        TechnicalSnapshot("BTC-USD", True, price=120, sma=100, trailing_return=0.03, volume_z=0),
        TechnicalSnapshot("BTC-USD", False, detail="upstream unavailable"),
    ],
)
def test_production_price_setup_also_requires_confirmation_snapshot(snapshot):
    universe = UniverseConfig(symbols={"BTC": {"product_id": "BTC-USD", "tier": "major"}})
    signals = SignalsConfig(
        tiers={
            "major": TierConfig(
                social_policy="ignored",
                min_relative_volume=1.5,
                retest_policy="preferred",
            )
        }
    )
    market_cfg = MarketConfig()
    market_cfg.confirmation.min_volume_zscore = 1.0
    engine = SignalEngine(
        make_strategy(),
        universe,
        signals,
        _SetupMarket(snapshot),  # type: ignore[arg-type]
        market_cfg,
    )

    assert engine.candidates([_score("BTC")]) == []


def test_zero_volume_z_floor_does_not_block_below_average_volume():
    universe = UniverseConfig(symbols={"BTC": {"product_id": "BTC-USD", "tier": "major"}})
    signals = SignalsConfig(
        tiers={
            "major": TierConfig(
                social_policy="ignored",
                min_relative_volume=1.5,
                retest_policy="preferred",
            )
        }
    )
    market_cfg = MarketConfig()
    assert market_cfg.confirmation.min_volume_zscore == 0.0
    snap = TechnicalSnapshot(
        "BTC-USD",
        True,
        price=120,
        sma=100,
        trailing_return=0.03,
        volume_z=-0.21,
    )
    engine = SignalEngine(
        make_strategy(),
        universe,
        signals,
        _SetupMarket(snap),  # type: ignore[arg-type]
        market_cfg,
    )
    assert engine._trend_ok(snap).passed  # noqa: SLF001 - gate unit test


def test_price_action_fail_closed_controls_missing_setup_data():
    universe = UniverseConfig(symbols={"BTC": {"product_id": "BTC-USD", "tier": "major"}})
    signals = SignalsConfig(tiers={"major": TierConfig(social_policy="ignored")})
    market = _SetupMarket(setup_data_available=False)
    fail_closed = MarketConfig()
    fail_closed.confirmation.enabled = False
    engine = SignalEngine(
        make_strategy(),
        universe,
        signals,
        market,  # type: ignore[arg-type]
        fail_closed,
    )
    assert engine.candidates([_score("BTC")]) == []

    fail_open = fail_closed.model_copy(deep=True)
    fail_open.price_action_fail_closed = False
    engine = SignalEngine(
        make_strategy(),
        universe,
        signals,
        market,  # type: ignore[arg-type]
        fail_open,
    )
    assert len(engine.candidates([_score("BTC")]) ) == 1
