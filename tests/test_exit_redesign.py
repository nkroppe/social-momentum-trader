"""Exit-profile snapshots, legacy continuity, and shared policy semantics."""

from __future__ import annotations

from datetime import timedelta

import pytest
from _helpers import make_store, make_strategy, make_universe
from sqlalchemy import inspect

from smt.config import ExitProfileConfig, RiskConfig, Settings, get_strategies
from smt.models import ExitReason, Trade, TradeStatus, utcnow
from smt.trader.exit_policy import (
    ExitActionKind,
    bar_step,
    first_partial_quantity,
    legacy_profile,
    quote_step,
    time_exit_reason,
)
from smt.trader.manager import TradeManager
from smt.trader.paper import PaperBroker
from smt.trader.signals import TradeCandidate


def test_deployed_nested_profiles_and_allocations_are_exact():
    strategies = {row.name: row for row in get_strategies().enabled()}
    intraday = strategies["intraday"]
    swing = strategies["swing"]
    bear = strategies["bear_rally"]

    assert (intraday.allocation, swing.allocation, bear.allocation) == (0.40, 0.40, 0.20)
    assert (
        intraday.label,
        intraday.partial_take_profit_fraction,
        intraday.chandelier_atr_mult,
        intraday.trail_granularity_seconds,
        intraday.stale_mfe_r,
        intraday.time_stop_hours,
    ) == ("intraday_trend_v2", 0.25, 2.5, 3_600, 0.5, 12)
    assert (
        swing.label,
        swing.partial_take_profit_fraction,
        swing.trail_granularity_seconds,
        swing.time_stop_hours,
    ) == ("swing_trend_v2", 0.25, 14_400, 120)
    assert (
        bear.mode,
        bear.advanced_exit_enabled,
        bear.label,
        bear.partial_take_profit_fraction,
        bear.chandelier_atr_mult,
        bear.trail_granularity_seconds,
    ) == ("bounded_target", True, "bear_reversion_v2", 0.50, 2.0, 900)


def test_flat_config_access_remains_compatible_and_nested_is_canonical():
    risk = RiskConfig(
        partial_take_profit_fraction=0.30,
        trail_granularity_seconds=3_600,
        stale_mfe_r=0.75,
        time_stop_hours=120,
    )
    assert risk.partial_take_profit_fraction == 0.30
    assert risk.exit_profile.partial_take_profit_fraction == 0.30
    assert risk.model_dump()["exit_profile"]["trail_granularity_seconds"] == 3_600
    assert "trail_granularity_seconds" not in risk.model_dump()
    canonical = RiskConfig(
        **{
            "exit": {
                "label": "canonical_exit",
                "mode": "bounded_target",
                "advanced_exit_enabled": True,
            }
        }
    )
    interim = RiskConfig(
        **{
            "exit_profile": {
                "label": "interim_exit_profile",
                "mode": "bounded_target",
                "advanced_exit_enabled": True,
            }
        }
    )
    assert canonical.label == "canonical_exit"
    assert canonical.mode == "bounded_target"
    assert canonical.advanced_exit_enabled is True
    assert interim.label == "interim_exit_profile"
    with pytest.raises(ValueError):
        ExitProfileConfig(time_stop_hours=121)


def test_trade_snapshot_and_fingerprint_are_persisted_and_immutable(tmp_path):
    store = make_store(tmp_path)
    broker = PaperBroker(seed=2)
    broker.set_price("BTC-USD", 100.0)
    strategy = make_strategy(
        exit_profile={
            "label": "snapshot_test",
            "mode": "partial_trail",
            "partial_take_profit_fraction": 0.25,
        }
    )
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
        config_fingerprint="a" * 64,
    )
    candidate = TradeCandidate(
        "BTC",
        "BTC-USD",
        5.0,
        20,
        3,
        "test",
        strategy.name,
        setup="breakout_close",
        entry_price=100.0,
        structure_stop=90.0,
        stop_pct=0.10,
    )
    trade = manager.open_position(candidate, 1_000.0, strategy)

    assert trade.config_fingerprint == "a" * 64
    assert trade.exit_profile_label == "snapshot_test"
    assert trade.exit_snapshot["partial_take_profit_fraction"] == 0.25
    trade.exit_profile_label = "mutated"
    with pytest.raises(ValueError, match="immutable"):
        store.update_trade(trade)


def test_migration_is_idempotent_and_adds_snapshot_columns(tmp_path):
    store = make_store(tmp_path)
    store.init_db()
    columns = {column["name"] for column in inspect(store.engine).get_columns("trades")}
    assert {"config_fingerprint", "exit_profile_label", "exit_snapshot"} <= columns


def test_legacy_open_trade_keeps_frozen_pre_redesign_profile(tmp_path):
    store = make_store(tmp_path)
    broker = PaperBroker(seed=3)
    broker.set_price("BTC-USD", 115.0)
    store.add_trade(
        Trade(
            ticker="BTC",
            strategy="intraday",
            product_id="BTC-USD",
            is_live=False,
            status=TradeStatus.OPEN,
            qty=10.0,
            original_qty=10.0,
            entry_price=100.0,
            entry_notional=1_000.0,
            take_profit=115.0,
            stop_loss=90.0,
            highest_price=100.0,
            initial_risk_per_unit=10.0,
            entry_fee_paid=6.0,
            fees_paid=6.0,
            time_stop_at=utcnow() + timedelta(hours=6),
            opened_at=utcnow(),
        )
    )
    current = make_strategy(
        partial_take_profit_fraction=0.25,
        chandelier_atr_mult=2.0,
        stale_mfe_r=0.5,
    )
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[current],
    )
    manager.manage_open_trades()

    persisted = store.open_trade_for("BTC", "intraday")
    assert persisted is not None
    assert persisted.qty == pytest.approx(5.0)
    assert persisted.exit_snapshot is None
    assert persisted.exit_profile_label == ""
    assert legacy_profile("swing").time_stop_hours == 48
    assert legacy_profile("bear_rally").chandelier_atr_mult == 2.5


def test_shared_steps_are_stop_first_and_partial_quantity_is_exact():
    profile = ExitProfileConfig(
        label="parity",
        partial_take_profit_fraction=0.25,
        partial_take_profit_r=1.5,
    )
    bar = bar_step(
        profile,
        low=89.0,
        high=116.0,
        stop_loss=90.0,
        take_profit=115.0,
        highest_price=100.0,
        trailing_stop=0.0,
        partial_taken=False,
        original_qty=10.0,
        current_qty=10.0,
    )
    quote = quote_step(
        profile,
        price=89.0,
        stop_loss=90.0,
        take_profit=115.0,
        highest_price=100.0,
        trailing_stop=0.0,
        partial_taken=False,
        original_qty=10.0,
        current_qty=10.0,
    )
    assert bar.actions[0].reason == quote.actions[0].reason == "STOP_LOSS"
    assert bar.actions[0].kind == ExitActionKind.CLOSE
    assert first_partial_quantity(10.0, 10.0, 0.25) == 2.5


def test_bear_bounded_target_label_still_runs_partial_and_trail(tmp_path):
    bear = next(row for row in get_strategies().enabled() if row.name == "bear_rally")
    broker = PaperBroker(seed=5)
    broker.set_price("BTC-USD", 100.0)
    store = make_store(tmp_path)
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[bear],
        config_fingerprint="b" * 64,
    )
    candidate = TradeCandidate(
        "BTC",
        "BTC-USD",
        5.0,
        20,
        3,
        "test",
        bear.name,
        setup="failed_breakdown",
        entry_price=100.0,
        structure_stop=90.0,
        stop_pct=0.10,
    )
    opened = manager.open_position(candidate, 1_000.0, bear)
    broker.set_price("BTC-USD", opened.take_profit)
    manager.manage_open_trades()

    partial = store.open_trade_for("BTC", bear.name)
    assert partial is not None
    assert partial.exit_snapshot["mode"] == "bounded_target"
    assert partial.exit_snapshot["advanced_exit_enabled"] is True
    assert partial.partial_taken is True
    assert partial.qty == pytest.approx(partial.original_qty * 0.50)
    assert partial.trailing_stop > partial.entry_price

    broker.set_price("BTC-USD", partial.trailing_stop - 1.0)
    manager.manage_open_trades()
    closed = store.closed_trades_for("BTC", bear.name)[-1]
    assert closed.exit_reason == ExitReason.TRAILING_STOP


def test_chandelier_requests_the_explicit_slower_atr_granularity(tmp_path):
    class CapturingMarket:
        requested: list[int] = []

        def candles(self, _product_id, seconds):
            self.requested.append(seconds)
            return []

    profile = ExitProfileConfig(
        label="slow_trail",
        trail_granularity_seconds=14_400,
    )
    market = CapturingMarket()
    manager = TradeManager(
        Settings(),
        make_universe(),
        make_store(tmp_path),
        PaperBroker(seed=4),
        market=market,  # type: ignore[arg-type]
    )
    trade = Trade(
        ticker="BTC",
        strategy="swing",
        product_id="BTC-USD",
        qty=1.0,
        entry_price=100.0,
        entry_notional=100.0,
        take_profit=120.0,
        stop_loss=90.0,
        highest_price=110.0,
        initial_risk_per_unit=10.0,
        time_stop_at=utcnow() + timedelta(hours=120),
    )
    assert manager._trail_atr(trade, profile) == 10.0  # noqa: SLF001
    assert market.requested == [14_400]


def test_stale_mfe_threshold_and_five_day_hard_stop_are_distinct():
    profile = ExitProfileConfig(
        label="five_day",
        time_stop_hours=120,
        stale_time_stop_hours=24,
        stale_mfe_r=0.5,
    )
    base = {
        "profile_value": profile,
        "entry_price": 100.0,
        "initial_risk_per_unit": 10.0,
        "partial_taken": False,
    }
    assert (
        time_exit_reason(
            **base,
            held_seconds=24 * 3_600,
            highest_price=104.99,
        )
        == "STALE_TIME_STOP"
    )
    assert (
        time_exit_reason(
            **base,
            held_seconds=24 * 3_600,
            highest_price=105.0,
        )
        is None
    )
    assert (
        time_exit_reason(
            **{**base, "partial_taken": True},
            held_seconds=120 * 3_600,
            highest_price=105.0,
        )
        == "TIME_STOP"
    )
