"""Tests for indicators and volatility-scaled exit levels."""

from __future__ import annotations

import pytest
from _helpers import make_store, make_strategy, make_universe

from smt.config import Settings
from smt.market.indicators import Candle, atr, sma, trailing_return, volume_zscore
from smt.trader.manager import TradeManager
from smt.trader.paper import PaperBroker
from smt.trader.signals import TradeCandidate


def _candles(closes: list[float], spread: float = 1.0, volume: float = 100.0) -> list[Candle]:
    return [
        Candle(ts=i * 3600, low=c - spread, high=c + spread, open=c, close=c, volume=volume)
        for i, c in enumerate(closes)
    ]


def test_indicators_on_flat_series():
    candles = _candles([100.0] * 30)
    assert atr(candles, 14) == 2.0
    assert sma(candles, 10) == 100.0
    assert trailing_return(candles, 5) == 0.0


def test_trailing_return_measures_move():
    candles = _candles([100.0, 100.0, 100.0, 110.0])
    assert trailing_return(candles, 3) == 0.10


def test_indicators_degrade_without_history():
    assert atr([], 14) == 0.0
    assert sma([], 10) == 0.0
    assert trailing_return(_candles([100.0]), 5) == 0.0
    assert volume_zscore(_candles([100.0, 101.0]), 24) == 0.0


def test_volume_zscore_flags_expansion():
    candles = _candles([100.0] * 30)
    spiked = candles[:-1] + [
        Candle(ts=999, low=99, high=101, open=100, close=100, volume=1000.0)
    ]
    assert volume_zscore(spiked, 24) > 3.0


def _manager(tmp_path) -> TradeManager:
    return TradeManager(
        Settings(paper_start_equity=5000),
        make_universe(),
        make_store(tmp_path),
        PaperBroker(seed=1),
    )


def test_atr_exits_scale_with_volatility(tmp_path):
    """The same rule must produce tight BTC exits and wide micro-cap exits."""
    mgr = _manager(tmp_path)
    st = make_strategy(exit_style="atr", atr_take_profit_mult=2.0, atr_stop_loss_mult=1.0)

    calm = TradeCandidate("BTC", "BTC-USD", 5.0, 20, 1, "x", st.name, atr_pct=0.01)
    wild = TradeCandidate("PUMP", "PUMP-USD", 5.0, 20, 1, "x", st.name, atr_pct=0.12)

    calm_tp, calm_sl, _ = mgr.exit_levels(100.0, calm, st)
    wild_tp, wild_sl, _ = mgr.exit_levels(100.0, wild, st)

    assert (100.0 - calm_sl) < (100.0 - wild_sl)
    assert (calm_tp - 100.0) < (wild_tp - 100.0)


def test_atr_stop_is_clamped(tmp_path):
    """An extreme ATR cannot push the stop past the configured ceiling."""
    mgr = _manager(tmp_path)
    st = make_strategy(exit_style="atr", atr_max_stop_pct=0.15, atr_stop_loss_mult=1.0)
    cand = TradeCandidate("PUMP", "PUMP-USD", 5.0, 20, 1, "x", st.name, atr_pct=0.90)

    _, sl, _ = mgr.exit_levels(100.0, cand, st)
    assert sl == 85.0


def test_exits_widen_with_holding_period(tmp_path):
    """A 48h swing must target further than a 6h intraday on the same asset."""
    mgr = _manager(tmp_path)
    intraday = make_strategy("intraday", exit_style="atr", time_stop_hours=6)
    swing = make_strategy("swing", exit_style="atr", time_stop_hours=48)
    cand = TradeCandidate("ZEC", "ZEC-USD", 5.0, 20, 1, "x", atr_pct=0.0076)

    intraday_tp, _, _ = mgr.exit_levels(100.0, cand, intraday)
    swing_tp, _, _ = mgr.exit_levels(100.0, cand, swing)
    assert swing_tp > intraday_tp

    # Volatility grows with the square root of time, not linearly.
    assert mgr.horizon_volatility(0.01, swing) == pytest.approx(0.01 * 48**0.5)


def test_take_profit_never_sits_inside_fees(tmp_path):
    """A target smaller than round-trip costs is raised to the fee floor."""
    mgr = _manager(tmp_path)
    st = make_strategy(
        exit_style="atr",
        atr_stop_loss_mult=1.0,
        atr_take_profit_mult=1.0,
        atr_min_stop_pct=0.001,
        assumed_fee_pct_per_side=0.006,
    )
    cand = TradeCandidate("BTC", "BTC-USD", 5.0, 20, 1, "x", st.name, atr_pct=0.0005)

    tp, _, note = mgr.exit_levels(100.0, cand, st)
    assert tp >= 100.0 * (1 + 3 * 0.006)
    assert "fee floor" in note


def test_fixed_exit_style_uses_percentages(tmp_path):
    mgr = _manager(tmp_path)
    st = make_strategy(exit_style="fixed", take_profit_pct=0.06, stop_loss_pct=0.03)
    cand = TradeCandidate("BTC", "BTC-USD", 5.0, 20, 1, "x", st.name, atr_pct=0.50)

    tp, sl, _ = mgr.exit_levels(100.0, cand, st)
    assert tp == 106.0
    assert sl == 97.0
