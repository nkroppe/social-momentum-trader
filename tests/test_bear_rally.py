"""Tests for the RISK-OFF bear_rally strategy."""

from __future__ import annotations

from _helpers import make_strategy, make_universe

from smt.config import (
    EntryRulesConfig,
    StrategiesConfig,
    StrategyConfirmationConfig,
    get_market,
    get_signals,
    get_strategies,
)
from smt.market import Candle
from smt.scorer import ScoreResult
from smt.trader.signals import SignalEngine, detect_price_setup


def _score(ticker: str = "BTC") -> ScoreResult:
    return ScoreResult(
        ticker=ticker,
        zscore=0.0,
        recent=0.0,
        baseline_mean=0.0,
        mentions_window=0,
        distinct_sources=0,
        distinct_authors=0,
        bullish_ratio=0.0,
        directional_posts=0,
        baseline_kind="event_trailing",
        reason="test",
    )


def _base_candles(count: int = 80, *, start: float = 100.0, step: float = -0.3) -> list[Candle]:
    """Downtrending tape suitable for relief-rally setups."""
    rows: list[Candle] = []
    for i in range(count):
        close = start + i * step
        rows.append(
            Candle(
                ts=i * 900,
                open=close + 0.05,
                high=close + 0.15,
                low=close - 0.20,
                close=close,
                volume=100.0,
            )
        )
    return rows


def _bear_entry() -> EntryRulesConfig:
    return EntryRulesConfig(
        setup_family="bear_rally",
        require_trigger_ema_stack=False,
        require_bias_ema_stack=False,
        rsi_min=0.0,
        rsi_oversold_max=35.0,
        rsi_reclaim_min=45.0,
        rsi_lookback_bars=8,
        allow_vwap_pullback=False,
        max_stop_pct=0.08,
        min_stop_pct=0.008,
        max_chase_return_pct=0.20,
    )


def test_strategies_yaml_loads_bear_rally():
    get_strategies.cache_clear()
    enabled = {s.name: s for s in get_strategies().enabled()}
    assert set(enabled) >= {"intraday", "swing", "bear_rally"}
    bear = enabled["bear_rally"]
    assert bear.regime_mode == "risk_off_only"
    assert bear.allowed_tickers == ["BTC", "ETH", "SOL"]
    assert bear.entry.setup_family == "bear_rally"
    assert bear.confirmation.require_above_sma is False
    total = sum(s.allocation for s in enabled.values())
    assert total <= 1.0 + 1e-9


def test_regime_mode_matrix():
    bull = make_strategy("intraday", regime_mode="risk_on_only")
    bear = make_strategy("bear_rally", regime_mode="risk_off_only")
    always = make_strategy("any", regime_mode="always")
    assert bull.regime_allows_entries(True) and not bull.regime_allows_entries(False)
    assert bear.regime_allows_entries(False, risk_off=True)
    assert not bear.regime_allows_entries(False, risk_off=False)
    assert not bear.regime_allows_entries(True, risk_off=False)
    assert always.regime_allows_entries(True) and always.regime_allows_entries(False)


def test_failed_breakdown_setup_detects_reclaim():
    rows = _base_candles()
    structure = rows[-(20 + 3 + 1) : -3]
    level = min(c.low for c in structure)
    # Sweep below the level, then reclaim.
    rows[-2] = Candle(
        ts=rows[-2].ts,
        open=level,
        high=level + 0.1,
        low=level - 1.0,
        close=level - 0.2,
        volume=250.0,
    )
    rows[-1] = Candle(
        ts=rows[-1].ts,
        open=level - 0.1,
        high=level + 0.8,
        low=level - 0.3,
        close=level + 0.5,
        volume=300.0,
    )
    bias = list(rows)
    tier = get_signals().tier("major")
    setup = detect_price_setup(rows, bias, _bear_entry(), tier, "bear_rally")
    assert setup is not None
    assert setup.name == "failed_breakdown"
    assert setup.structure_stop < setup.entry_price


def test_rsi_reclaim_rejects_bearish_reclaim_candle():
    rows = _base_candles()
    # Red candle cannot be an RSI reclaim entry.
    rows[-1] = Candle(
        ts=rows[-1].ts,
        open=rows[-1].close + 1.0,
        high=rows[-1].close + 1.2,
        low=rows[-1].close - 0.5,
        close=rows[-1].close - 0.2,
        volume=300.0,
    )
    entry = _bear_entry()
    entry.allow_failed_breakdown = False
    entry.allow_rs_bounce = False
    from smt.trader.signals import _rsi_reclaim_setup

    assert _rsi_reclaim_setup(rows, entry, 1.0) is None


class _RegimeMarket:
    def __init__(self, *, risk_on: bool = False, risk_off: bool = False):
        self._risk_on = risk_on
        self._risk_off = risk_off

    def regime_ok(self) -> tuple[bool, str]:
        return self._risk_on, "test regime"

    def regime_assessment(self) -> tuple[bool, bool, str]:
        return self._risk_on, self._risk_off, "test regime"

    def candles(self, product_id: str, granularity: int = 3600) -> list[Candle]:
        return []

    def snapshot(self, *args, **kwargs):
        return None


def test_bear_rally_engine_only_enters_risk_off():
    universe = make_universe()
    market_cfg = get_market().model_copy(deep=True)
    market_cfg.price_action_enabled = False
    market_cfg.price_action_fail_closed = False
    market_cfg.confirmation.enabled = False
    market_cfg.confirmation.fail_closed = False

    bear = make_strategy(
        "bear_rally",
        allocation=0.2,
        regime_mode="risk_off_only",
        allowed_tickers=["BTC", "SOL"],
        confirmation=StrategyConfirmationConfig(require_above_sma=False),
        entry=EntryRulesConfig(setup_family="bear_rally", require_trigger_ema_stack=False),
        signal_min_zscore=0.0,
        signal_min_mentions=0,
        signal_min_distinct_sources=0,
        signal_min_distinct_authors=0,
        signal_min_bullish_ratio=0.0,
    )
    scores = [_score("BTC"), _score("SOL")]

    off_engine = SignalEngine(
        bear, universe, market=_RegimeMarket(risk_off=True), market_cfg=market_cfg
    )
    on_engine = SignalEngine(
        bear, universe, market=_RegimeMarket(risk_on=True), market_cfg=market_cfg
    )
    # Above SMA with structure block is not RISK-OFF.
    structure_blocked = SignalEngine(
        bear, universe, market=_RegimeMarket(), market_cfg=market_cfg
    )

    assert off_engine.candidates(scores)
    assert on_engine.candidates(scores) == []
    assert structure_blocked.candidates(scores) == []

    blocked = on_engine.evaluations(scores)
    assert all(row.outcome_status == "regime_blocked" for row in blocked)
    assert all(row.regime_status == "blocked" for row in blocked)


def test_bear_rally_filters_non_allowlisted_tickers():
    universe = make_universe()
    market_cfg = get_market().model_copy(deep=True)
    market_cfg.price_action_enabled = False
    market_cfg.price_action_fail_closed = False
    market_cfg.confirmation.enabled = False
    market_cfg.confirmation.fail_closed = False
    market_cfg.regime.enabled = False

    bear = make_strategy(
        "bear_rally",
        allocation=0.2,
        regime_mode="always",
        allowed_tickers=["BTC"],
        signal_min_zscore=0.0,
        signal_min_mentions=0,
        signal_min_distinct_sources=0,
        signal_min_distinct_authors=0,
        signal_min_bullish_ratio=0.0,
    )
    engine = SignalEngine(bear, universe, market_cfg=market_cfg)
    rows = engine.evaluations([_score("BTC"), _score("SOL")])
    by_ticker = {row.ticker: row for row in rows}
    assert by_ticker["BTC"].outcome_status == "candidate"
    assert by_ticker["SOL"].outcome_status == "filtered"


def test_allocation_sum_with_bear_rally_ok():
    strategies = StrategiesConfig(
        strategies={
            "intraday": make_strategy("intraday", allocation=0.4),
            "swing": make_strategy("swing", allocation=0.4),
            "bear_rally": make_strategy(
                "bear_rally",
                allocation=0.2,
                regime_mode="risk_off_only",
                entry=EntryRulesConfig(setup_family="bear_rally"),
            ),
        }
    )
    assert sum(s.allocation for s in strategies.enabled()) == 1.0
