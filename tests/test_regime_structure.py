"""BTC regime: daily SMA50 plus optional 4h consecutive lower-lows gate."""

from __future__ import annotations

from smt.config import MarketConfig, RegimeConfig
from smt.market import Candle
from smt.market.data import MarketData, _no_consecutive_lower_lows


def _daily_above_sma(count: int = 60, start: float = 100.0) -> list[Candle]:
    rows: list[Candle] = []
    price = start
    for i in range(count):
        price *= 1.01
        rows.append(
            Candle(
                ts=1_700_000_000 + i * 86_400,
                open=price * 0.99,
                high=price * 1.01,
                low=price * 0.98,
                close=price,
                volume=1_000.0,
            )
        )
    return rows


def _four_hour(lows: list[float], base_ts: int = 1_700_000_000) -> list[Candle]:
    rows: list[Candle] = []
    for i, low in enumerate(lows):
        close = low + 1.0
        rows.append(
            Candle(
                ts=base_ts + i * 14_400,
                open=close,
                high=close + 1.0,
                low=low,
                close=close,
                volume=100.0,
            )
        )
    return rows


def test_no_consecutive_lower_lows_detects_breakdown():
    ok, detail = _no_consecutive_lower_lows(_four_hour([10.0, 9.0, 8.0]), 3)
    assert not ok
    assert "lower lows" in detail


def test_no_consecutive_lower_lows_allows_mixed_structure():
    ok, detail = _no_consecutive_lower_lows(_four_hour([10.0, 8.0, 9.0]), 3)
    assert ok
    assert "not consecutively lower" in detail


class _StubMarket(MarketData):
    def __init__(self, daily: list[Candle], four_hour: list[Candle], cfg: MarketConfig):
        self.cfg = cfg
        self._daily = daily
        self._four = four_hour
        self._client = None  # unused

    def candles(self, product_id: str, granularity: int | None = None) -> list[Candle]:
        gran = granularity or self.cfg.candle_granularity_seconds
        if gran == 86_400:
            return list(self._daily)
        if gran == 14_400:
            return list(self._four)
        return []

    def close(self) -> None:
        return None


def test_regime_blocks_when_4h_prints_lower_lows():
    cfg = MarketConfig(
        regime=RegimeConfig(
            enabled=True,
            require_no_lower_lows=True,
            structure_granularity_seconds=14_400,
            structure_lower_lows_bars=3,
        )
    )
    market = _StubMarket(_daily_above_sma(), _four_hour([30.0, 20.0, 10.0]), cfg)
    ok, detail = market.regime_ok()
    assert not ok
    assert "lower lows" in detail
    risk_on, risk_off, assessment = market.regime_assessment()
    assert not risk_on
    assert not risk_off
    assert "lower lows" in assessment


def test_regime_risk_off_when_below_sma():
    daily = _daily_above_sma()
    # Force the last close below the SMA50 without needing a full downtrend history.
    trend_proxy = sum(c.close for c in daily[-50:]) / 50.0
    last = daily[-1]
    daily[-1] = Candle(
        ts=last.ts,
        open=last.open,
        high=last.high,
        low=min(last.low, trend_proxy - 1.0),
        close=trend_proxy - 1.0,
        volume=last.volume,
    )
    cfg = MarketConfig(
        regime=RegimeConfig(
            enabled=True,
            require_no_lower_lows=True,
            structure_granularity_seconds=14_400,
            structure_lower_lows_bars=3,
        )
    )
    market = _StubMarket(daily, _four_hour([10.0, 12.0, 11.0]), cfg)
    risk_on, risk_off, detail = market.regime_assessment()
    assert not risk_on
    assert risk_off
    assert "below SMA" in detail


def test_regime_passes_when_sma_ok_and_4h_not_breaking_down():
    cfg = MarketConfig(
        regime=RegimeConfig(
            enabled=True,
            require_no_lower_lows=True,
            structure_granularity_seconds=14_400,
            structure_lower_lows_bars=3,
        )
    )
    market = _StubMarket(_daily_above_sma(), _four_hour([10.0, 12.0, 11.0]), cfg)
    ok, detail = market.regime_ok()
    assert ok
    assert "above SMA" in detail
    assert "not consecutively lower" in detail
    risk_on, risk_off, _ = market.regime_assessment()
    assert risk_on
    assert not risk_off
