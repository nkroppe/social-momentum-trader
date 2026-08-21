"""Pure exit-policy, configuration, attribution, and replay parity tests."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest
from _helpers import make_store, make_strategy, make_universe
from sqlalchemy import inspect

from smt.backtest import replay_exit_bars
from smt.config import ExitProfileConfig, RiskConfig, Settings, StrategyConfig
from smt.market import Candle
from smt.models import ExitReason, Trade, TradeStatus, utcnow
from smt.policy import strategy_exit_snapshot, trading_policy_identity
from smt.trader.exit_policy import (
    ExitPlan,
    ExitState,
    build_exit_plan,
    step_bar,
    step_quote,
)
from smt.trader.manager import TradeManager
from smt.trader.paper import PaperBroker
from smt.trader.signals import TradeCandidate


def _profile(**updates) -> ExitProfileConfig:
    values = {
        "label": "test_v2",
        "partial_take_profit_fraction": 0.25,
        "partial_take_profit_r": 1.5,
        "chandelier_atr_mult": 2.5,
        "trail_granularity_seconds": 3_600,
        "stale_time_stop_hours": 4,
        "stale_mfe_r": 0.5,
        "time_stop_hours": 12,
    }
    values.update(updates)
    return ExitProfileConfig(**values)


def _plan(profile: ExitProfileConfig | None = None) -> ExitPlan:
    opened = utcnow()
    return build_exit_plan(
        entry_price=100.0,
        structure_stop=90.0,
        structure_stop_pct=0.10,
        atr_pct=0.01,
        horizon_vol_pct=0.03,
        assumed_fee_pct_per_side=0.006,
        profile=profile or _profile(),
        opened_at=opened,
    )


def _state(**updates) -> ExitState:
    values = {
        "entry_price": 100.0,
        "qty": 10.0,
        "original_qty": 10.0,
        "highest_price": 100.0,
        "partial_taken": False,
        "trailing_stop": 0.0,
    }
    values.update(updates)
    return ExitState(**values)


def test_flat_and_nested_exit_config_are_equivalent():
    flat = RiskConfig(time_stop_hours=12, stale_time_stop_hours=4, stale_mfe_r=0.5)
    nested = RiskConfig(
        exit={"time_stop_hours": 12, "stale_time_stop_hours": 4, "stale_mfe_r": 0.5}
    )
    assert flat.exit == nested.exit
    assert flat.time_stop_hours == 12

    strategy = make_strategy(
        exit={"label": "nested", "time_stop_hours": 120, "stale_time_stop_hours": 24}
    )
    assert strategy.exit.label == "nested"
    assert strategy.time_stop_hours == 120


def test_policy_fingerprint_and_snapshot_change_with_exit_profile():
    control = make_strategy()
    challenger = make_strategy(exit=control.exit.model_copy(update={"time_stop_hours": 12}))
    assert trading_policy_identity([control]).fingerprint != trading_policy_identity(
        [challenger]
    ).fingerprint
    assert json.loads(strategy_exit_snapshot(challenger))["time_stop_hours"] == 12


def test_structure_plan_uses_fill_risk_and_selected_partial_r():
    plan = _plan()
    assert plan.stop_loss == 90.0
    assert plan.take_profit == 115.0
    assert plan.initial_risk_per_unit == 10.0
    assert plan.time_stop_at - plan.stale_stop_at == timedelta(hours=8)


def test_replay_bar_is_stop_first_when_stop_and_target_are_touched():
    plan = _plan()
    bar = Candle(ts=1, open=100, high=116, low=89, close=110, volume=1)
    result = step_bar(state=_state(), plan=plan, bar=bar, atr_abs=2.0, now=utcnow())
    assert result.action.kind == "close"
    assert result.action.reason == ExitReason.STOP_LOSS
    assert result.action.reference_price == 90.0


def test_partial_is_25_percent_and_trail_uses_supplied_slower_atr():
    plan = _plan()
    result = step_quote(
        state=_state(),
        plan=plan,
        price=116.0,
        atr_abs=4.0,
        now=utcnow(),
    )
    assert result.action.kind == "partial"
    assert result.action.qty == pytest.approx(2.5)
    assert result.trailing_stop == pytest.approx(106.0)


def test_stale_exit_requires_mfe_below_configured_progress():
    plan = _plan()
    now = plan.stale_stop_at + timedelta(seconds=1)
    stale = step_quote(
        state=_state(highest_price=104.9),
        plan=plan,
        price=102.0,
        atr_abs=2.0,
        now=now,
    )
    assert stale.action.reason == ExitReason.STALE_TIME_STOP

    progressing = step_quote(
        state=_state(highest_price=105.0),
        plan=plan,
        price=102.0,
        atr_abs=2.0,
        now=now,
    )
    assert progressing.action.kind == "none"


def test_hard_time_stop_supports_120_hour_swing():
    profile = _profile(time_stop_hours=120, stale_time_stop_hours=24)
    plan = _plan(profile)
    result = step_quote(
        state=_state(highest_price=106.0),
        plan=plan,
        price=103.0,
        atr_abs=2.0,
        now=plan.time_stop_at,
    )
    assert result.action.reason == ExitReason.TIME_STOP


def test_trade_persists_policy_snapshot_and_schema_columns(tmp_path):
    store = make_store(tmp_path)
    broker = PaperBroker(seed=1)
    broker.set_price("BTC-USD", 100.0)
    strategy = make_strategy(
        exit=_profile(label="intraday_trend_v2"),
    )
    fingerprint = trading_policy_identity([strategy]).fingerprint
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
        policy_fingerprint=fingerprint,
    )
    trade = manager.open_position(
        TradeCandidate(
            "BTC",
            "BTC-USD",
            0,
            0,
            0,
            "test",
            setup="breakout_close",
            entry_price=100.0,
            structure_stop=90.0,
            stop_pct=0.10,
        ),
        1_000,
        strategy,
    )
    assert trade.config_fingerprint == fingerprint
    assert trade.exit_profile_label == "intraday_trend_v2"
    assert json.loads(trade.exit_snapshot)["partial_take_profit_fraction"] == 0.25
    broker.set_price("BTC-USD", 115.0)
    manager.manage_open_trades()
    managed = store.open_trade_for("BTC", strategy.name)
    assert managed is not None
    assert managed.qty == pytest.approx(7.5)
    columns = {column["name"] for column in inspect(store.engine).get_columns("trades")}
    assert {"config_fingerprint", "exit_profile_label", "exit_snapshot"} <= columns


def test_paper_manager_matches_pure_quote_action_sequence(tmp_path):
    store = make_store(tmp_path)
    broker = PaperBroker(seed=2)
    broker.set_price("BTC-USD", 100.0)
    strategy = make_strategy(exit=_profile())
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
    )
    trade = manager.open_position(
        TradeCandidate(
            "BTC",
            "BTC-USD",
            0,
            0,
            0,
            "test",
            setup="breakout_retest",
            entry_price=100.0,
            structure_stop=90.0,
            stop_pct=0.10,
        ),
        1_000,
        strategy,
    )
    plan = _plan(strategy.exit)
    state = _state()

    partial = step_quote(
        state=state,
        plan=plan,
        price=115.0,
        atr_abs=10.0,
        now=trade.opened_at,
    )
    broker.set_price("BTC-USD", 115.0)
    manager.manage_open_trades()
    paper = store.open_trade_for("BTC", strategy.name)
    assert paper is not None
    assert partial.action.kind == "partial"
    assert paper.qty == pytest.approx(state.qty - partial.action.qty)
    assert paper.trailing_stop == pytest.approx(partial.trailing_stop)

    state = ExitState(
        entry_price=100.0,
        qty=paper.qty,
        original_qty=10.0,
        highest_price=paper.highest_price,
        partial_taken=True,
        trailing_stop=paper.trailing_stop,
    )
    ratchet = step_quote(
        state=state,
        plan=plan,
        price=130.0,
        atr_abs=10.0,
        now=trade.opened_at,
    )
    broker.set_price("BTC-USD", 130.0)
    manager.manage_open_trades()
    paper = store.open_trade_for("BTC", strategy.name)
    assert paper is not None
    assert ratchet.action.kind == "none"
    assert paper.trailing_stop == pytest.approx(ratchet.trailing_stop)

    state = ExitState(
        entry_price=100.0,
        qty=paper.qty,
        original_qty=10.0,
        highest_price=paper.highest_price,
        partial_taken=True,
        trailing_stop=paper.trailing_stop,
    )
    close = step_quote(
        state=state,
        plan=plan,
        price=105.0,
        atr_abs=10.0,
        now=trade.opened_at,
    )
    broker.set_price("BTC-USD", 105.0)
    manager.manage_open_trades()
    closed = store.closed_trades_for("BTC", strategy.name)[-1]
    assert close.action.reason == ExitReason.TRAILING_STOP
    assert closed.exit_reason == close.action.reason
    assert closed.exit_price == close.action.reference_price


def test_replay_adapter_emits_same_partial_and_trailing_actions():
    plan = _plan()
    result = replay_exit_bars(
        plan=plan,
        initial_state=_state(),
        bars=[
            (
                plan.stale_stop_at - timedelta(hours=3),
                Candle(1, 100, 115, 100, 115, 1),
                10.0,
            ),
            (
                plan.stale_stop_at - timedelta(hours=2),
                Candle(2, 115, 130, 110, 130, 1),
                10.0,
            ),
            (
                plan.stale_stop_at - timedelta(hours=1),
                Candle(3, 130, 130, 105, 105, 1),
                10.0,
            ),
        ],
    )
    assert [(action.kind, action.reason, action.reference_price, action.qty) for action in result.actions] == [
        ("partial", ExitReason.TAKE_PROFIT, 115.0, 2.5),
        ("close", ExitReason.TRAILING_STOP, 105.0, 7.5),
    ]
    assert result.state.qty == 0.0


def test_strategy_model_accepts_legacy_flat_exit_overrides():
    risk = RiskConfig()
    inherited = {
        "name": "flat",
        "allocation": 1.0,
        "exit": risk.exit.model_dump(),
        "time_stop_hours": 24,
        "stale_time_stop_hours": 4,
    }
    for field in (
        "signal_min_zscore",
        "signal_min_distinct_sources",
        "signal_min_distinct_authors",
        "signal_min_mentions",
        "signal_min_bullish_ratio",
        "scorer_bucket_minutes",
        "scorer_lookback_buckets",
        "scorer_seasonal_days",
        "scorer_seasonal_min_history_hours",
        "confirm_lookback_hours",
        "confirm_min_return_pct",
        "max_position_pct",
        "risk_per_trade_pct",
        "max_open_positions",
        "max_trades_per_day",
        "daily_loss_halt_pct",
        "weekly_loss_halt_pct",
        "cooldown_minutes_after_stop",
        "min_order_notional_usd",
        "assumed_fee_pct_per_side",
    ):
        inherited[field] = getattr(risk, field)
    strategy = StrategyConfig(**inherited)
    assert strategy.time_stop_hours == 24


def test_legacy_trade_table_migrates_policy_columns(tmp_path):
    path = tmp_path / "legacy.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                qty FLOAT NOT NULL,
                entry_price FLOAT NOT NULL,
                stop_loss FLOAT NOT NULL,
                fees_paid FLOAT DEFAULT 0.0
            )
            """
        )
        connection.execute(
            "INSERT INTO trades (qty, entry_price, stop_loss, fees_paid) "
            "VALUES (2.0, 100.0, 90.0, 1.0)"
        )

    from smt.store import Store

    store = Store(f"sqlite:///{path.as_posix()}")
    store.init_db()
    columns = {column["name"] for column in inspect(store.engine).get_columns("trades")}
    assert {"config_fingerprint", "exit_profile_label", "exit_snapshot"} <= columns
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT original_qty, highest_price, initial_risk_per_unit, exit_snapshot "
            "FROM trades"
        ).fetchone()
    assert row == (2.0, 100.0, 10.0, "{}")


def test_open_trade_without_snapshot_keeps_legacy_exit_semantics(tmp_path):
    store = make_store(tmp_path)
    trade = store.add_trade(
        Trade(
            ticker="BTC",
            strategy="swing",
            product_id="BTC-USD",
            status=TradeStatus.OPEN,
            qty=10,
            original_qty=10,
            entry_price=100,
            entry_notional=1_000,
            take_profit=120,
            stop_loss=90,
            highest_price=100,
            initial_risk_per_unit=10,
            exit_snapshot="{}",
            time_stop_at=utcnow() + timedelta(hours=48),
        )
    )
    new_swing = make_strategy(
        "swing",
        exit=_profile(
            label="swing_trend_v2",
            time_stop_hours=120,
            stale_time_stop_hours=24,
            partial_take_profit_fraction=0.25,
        ),
    )
    manager = TradeManager(
        Settings(),
        make_universe(),
        store,
        PaperBroker(seed=3),
        strategies=[new_swing],
    )
    preserved = manager._profile_for(trade)
    assert preserved.label == "legacy_swing_v1"
    assert preserved.time_stop_hours == 48
    assert preserved.partial_take_profit_fraction == pytest.approx(0.50)
