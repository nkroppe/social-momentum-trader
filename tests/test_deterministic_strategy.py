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
    Settings,
    SignalsConfig,
    TierConfig,
    UniverseConfig,
    get_strategies,
)
from smt.market import (
    Candle,
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


def test_intraday_and_swing_rules_are_structurally_distinct():
    strategies = {strategy.name: strategy for strategy in get_strategies().enabled()}
    assert strategies["intraday"].entry.trigger_granularity_seconds == 900
    assert strategies["intraday"].entry.bias_granularity_seconds == 3_600
    assert strategies["intraday"].entry.allow_vwap_pullback
    assert not strategies["intraday"].entry.require_compression
    assert strategies["swing"].entry.trigger_granularity_seconds == 3_600
    assert strategies["swing"].entry.bias_granularity_seconds == 14_400
    assert strategies["swing"].entry.require_compression
    assert not strategies["swing"].entry.allow_vwap_pullback


class _SetupMarket:
    def candles(self, _product: str, granularity: int | None = None) -> list[Candle]:
        return _direct_breakout() if granularity == 900 else _candles()

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
        }
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


def _candidate(**overrides) -> TradeCandidate:
    values = {
        "ticker": "BTC",
        "product_id": "BTC-USD",
        "zscore": 0.0,
        "mentions": 0,
        "sources": 0,
        "reason": "price",
        "tier": "major",
        "stop_pct": 0.02,
        "conviction": 1.0,
        "size_multiplier": 1.0,
    }
    values.update(overrides)
    return TradeCandidate(**values)


def test_risk_budget_sizing_uses_stop_and_never_exceeds_hard_cap(tmp_path):
    cfg = MarketConfig()
    cfg.sizing.enabled = False
    gate = RiskGate(make_store(tmp_path), market_cfg=cfg)
    strategy = make_strategy(max_position_pct=0.50, risk_per_trade_pct=0.005)

    tight, _ = gate.size_position(_candidate(stop_pct=0.02), strategy, 10_000)
    wide, _ = gate.size_position(_candidate(stop_pct=0.05), strategy, 10_000)
    boosted, _ = gate.size_position(
        _candidate(stop_pct=0.001, conviction=2.0, size_multiplier=2.0),
        strategy,
        10_000,
    )
    assert tight == 2_500
    assert wide == 1_000
    assert boosted == 5_000


def _open_trade(store, strategy: str = "intraday") -> Trade:
    return store.add_trade(
        Trade(
            ticker="BTC",
            strategy=strategy,
            product_id="BTC-USD",
            status=TradeStatus.OPEN,
            qty=10.0,
            original_qty=10.0,
            entry_price=100.0,
            entry_notional=1_000.0,
            take_profit=115.0,
            stop_loss=90.0,
            highest_price=100.0,
            initial_risk_per_unit=10.0,
            time_stop_at=utcnow() + timedelta(hours=6),
        )
    )


def test_loss_halt_includes_unrealized_and_fails_closed_on_quote_errors(tmp_path):
    store = make_store(tmp_path)
    _open_trade(store)
    strategy = make_strategy(daily_loss_halt_pct=-0.05)

    gate = RiskGate(store, mark_price=lambda _product: 90.0)
    halted, reason = gate.portfolio_halted(strategy, 1_000.0)
    assert halted
    assert "daily loss halt" in reason

    failed_quote = RiskGate(store, mark_price=lambda _product: None)
    halted, reason = failed_quote.portfolio_halted(strategy, 1_000.0)
    assert halted
    assert "conservative" in reason


def test_loss_halt_uses_persisted_period_equity_not_lifetime_open_pnl(tmp_path):
    store = make_store(tmp_path)
    trade = _open_trade(store)
    trade.opened_at = utcnow() - timedelta(days=8)
    store.update_trade(trade)
    mark = {"price": 90.0}
    strategy = make_strategy(daily_loss_halt_pct=-0.05, weekly_loss_halt_pct=-0.12)
    gate = RiskGate(store, mark_price=lambda _product: mark["price"])

    halted, _ = gate.portfolio_halted(strategy, 1_000.0)
    assert not halted  # the old -$100 is part of the first observed baseline

    mark["price"] = 80.0
    halted, reason = gate.portfolio_halted(strategy, 1_000.0)
    assert halted
    assert "daily loss halt" in reason


def test_fresh_quote_rejects_stale_setup_before_sizing(tmp_path):
    store = make_store(tmp_path)
    strategy = make_strategy()
    gate = RiskGate(store, mark_price=lambda _product: 110.0)
    candidate = _candidate(entry_price=100.0, structure_stop=90.0, stop_pct=0.10)
    decision = gate.evaluate(candidate, strategy, 5_000.0, 5_000.0)
    assert not decision.approved
    assert "setup stale" in decision.reason


def test_paper_fill_risk_breach_is_immediately_unwound_and_recorded(tmp_path):
    class GapBroker:
        name = "paper"
        server_side_brackets = False

        def current_price(self, _product):
            return 100.0

        def open_long(self, _product, notional, _tp, _sl):
            return Fill("buy", 110.0, notional / 110.0, 1.0)

        def close_long(self, _product, qty):
            return Fill("sell", 109.0, qty, 1.0)

    store = make_store(tmp_path)
    strategy = make_strategy()
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        GapBroker(),
        strategies=[strategy],
    )
    trade = manager.open_position(
        _candidate(
            setup="breakout_close",
            entry_price=100.0,
            structure_stop=90.0,
            stop_pct=0.10,
        ),
        1_000.0,
        strategy,
        risk_budget_usd=100.0,
    )
    assert trade.status == TradeStatus.CLOSED
    assert trade.exit_reason == ExitReason.ENTRY_RISK
    assert store.count_open_trades(strategy.name) == 0


def _advanced_manager(tmp_path):
    store = make_store(tmp_path)
    broker = PaperBroker(seed=1)
    strategy = make_strategy(
        advanced_exit_enabled=True,
        partial_take_profit_fraction=0.5,
        partial_take_profit_r=1.5,
        chandelier_atr_mult=3.0,
    )
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
    )
    return store, broker, strategy, manager


def test_partial_then_chandelier_exit_ratchets_and_accounts_for_all_fees(tmp_path):
    store, broker, strategy, manager = _advanced_manager(tmp_path)
    broker.set_price("BTC-USD", 100.0)
    trade = manager.open_position(
        _candidate(
            setup="breakout_retest",
            entry_price=100.0,
            structure_stop=90.0,
            stop_pct=0.10,
        ),
        1_000.0,
        strategy,
    )

    broker.set_price("BTC-USD", 115.0)
    manager.manage_open_trades()
    trade = store.open_trade_for("BTC", strategy.name)
    assert trade is not None
    assert trade.partial_taken
    assert trade.qty == pytest.approx(5.0)
    assert trade.partial_realized_pnl == pytest.approx(68.55)
    assert manager.equity() == pytest.approx(5_143.55)

    broker.set_price("BTC-USD", 130.0)
    manager.manage_open_trades()
    trade = store.open_trade_for("BTC", strategy.name)
    assert trade is not None
    first_trail = trade.trailing_stop
    assert first_trail == pytest.approx(100.0)

    broker.set_price("BTC-USD", 125.0)
    manager.manage_open_trades()
    assert store.open_trade_for("BTC", strategy.name).trailing_stop == first_trail

    broker.set_price("BTC-USD", 99.0)
    manager.manage_open_trades()
    closed = store.closed_trades_for("BTC", strategy.name)[-1]
    assert closed.exit_reason == ExitReason.TRAILING_STOP
    assert closed.fees_paid == pytest.approx(12.42)
    assert closed.realized_pnl == pytest.approx(57.58)


def test_stale_trade_closes_early_when_it_never_reaches_one_r(tmp_path):
    store, broker, strategy, manager = _advanced_manager(tmp_path)
    broker.set_price("BTC-USD", 100.0)
    trade = manager.open_position(
        _candidate(setup="breakout_close", structure_stop=90.0, stop_pct=0.10),
        1_000.0,
        strategy,
    )
    trade.opened_at = utcnow() - timedelta(hours=strategy.stale_time_stop_hours + 1)
    trade.highest_price = 105.0
    store.update_trade(trade)

    broker.set_price("BTC-USD", 102.0)
    manager.manage_open_trades()
    closed = store.closed_trades_for("BTC", strategy.name)[-1]
    assert closed.exit_reason == ExitReason.STALE_TIME_STOP
    assert closed.setup == "breakout_close"


def test_schema_migration_fields_and_live_preflight_block(tmp_path):
    store = make_store(tmp_path)
    columns = {column["name"] for column in inspect(store.engine).get_columns("trades")}
    assert {
        "original_qty",
        "highest_price",
        "initial_risk_per_unit",
        "partial_taken",
        "partial_realized_pnl",
        "trailing_stop",
        "setup",
    } <= columns
    live = {result.name: result for result in run_preflight("live")}
    assert not live["advanced_exit_live_parity"].passed


def test_sqlite_legacy_two_column_dedup_is_rebuilt(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE social_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source VARCHAR(32) NOT NULL,
                external_id VARCHAR(128) NOT NULL,
                ticker VARCHAR(16) NOT NULL,
                author VARCHAR(128) DEFAULT '',
                text TEXT DEFAULT '',
                url VARCHAR(512) DEFAULT '',
                weight FLOAT DEFAULT 1.0,
                sentiment FLOAT DEFAULT 0.0,
                author_followers INTEGER DEFAULT 0,
                text_hash VARCHAR(32) DEFAULT '',
                created_at DATETIME NOT NULL,
                ingested_at DATETIME,
                CONSTRAINT uq_source_extid UNIQUE (source, external_id)
            )
            """
        )
    store = Store(f"sqlite:///{path.as_posix()}")
    store.init_db()
    now = utcnow()
    events = [
        SocialEvent(
            source="x",
            external_id="same-post",
            ticker=ticker,
            text=f"${ticker}",
            created_at=now,
        )
        for ticker in ("SOL", "HYPE")
    ]
    assert store.add_events(events) == 2
