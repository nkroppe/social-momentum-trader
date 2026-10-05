"""Forward-only shadow entry gates: snapshot flags, unchanged order flow, report math."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from _helpers import make_store, make_strategy, make_universe

from smt.config import OpsConfig, Settings, ShadowGatesConfig, get_ops
from smt.models import Trade, TradeStatus, utcnow
from smt.ops.reports import (
    GEN8_CONFIG_FINGERPRINT_PREFIX,
    MFE_EVENT_SNAPSHOT_KEYS,
    SHADOW_GATE_SNAPSHOT_KEYS,
    build_weekly_report,
    format_shadow_gate_split,
    format_shadow_gate_summary,
    shadow_gate_summary,
)
from smt.trader.broker import Fill
from smt.trader.exit_policy import fee_hurdle_r, resolve_profile
from smt.trader.manager import TradeManager
from smt.trader.paper import PaperBroker
from smt.trader.signals import TradeCandidate


def _candidate(**overrides) -> TradeCandidate:
    values = dict(
        ticker="BTC",
        product_id="BTC-USD",
        zscore=5.0,
        mentions=20,
        sources=3,
        reason="test",
        strategy="intraday",
        setup="breakout_close",
        entry_price=100.0,
        structure_stop=90.0,
        stop_pct=0.10,
    )
    values.update(overrides)
    return TradeCandidate(**values)


def _manager(
    tmp_path,
    *,
    strategy=None,
    extra_open: list[Trade] | None = None,
    shadow_gate_fee_hurdle_r_max: float = 0.5,
    seed: int = 3,
) -> tuple[TradeManager, PaperBroker, object, object]:
    store = make_store(tmp_path)
    for trade in extra_open or []:
        store.add_trade(trade)
    broker = PaperBroker(seed=seed)
    broker.set_price("BTC-USD", 100.0)
    strategy = strategy or make_strategy()
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
        shadow_gate_fee_hurdle_r_max=shadow_gate_fee_hurdle_r_max,
    )
    return manager, broker, store, strategy


def _open_holder(*, ticker="BTC", strategy="swing") -> Trade:
    return Trade(
        ticker=ticker,
        strategy=strategy,
        product_id=f"{ticker}-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=100.0,
        take_profit=110.0,
        stop_loss=90.0,
        time_stop_at=utcnow(),
        status=TradeStatus.OPEN,
    )


def test_shadow_keys_do_not_collide_with_existing_snapshot_keys():
    profile_keys = set(resolve_profile(make_strategy().exit_profile).snapshot())
    reserved = (
        profile_keys
        | set(MFE_EVENT_SNAPSHOT_KEYS)
        | {
            "fee_hurdle_r",
            "fee_hurdle_pct_per_side",
        }
    )
    assert set(SHADOW_GATE_SNAPSHOT_KEYS).isdisjoint(reserved)


def test_ops_shadow_gates_defaults_without_yaml_key():
    get_ops.cache_clear()
    ops = get_ops()
    assert ops.shadow_gates.fee_hurdle_r_max == pytest.approx(0.5)
    assert ops.shadow_gates.enabled is True
    assert OpsConfig().shadow_gates == ShadowGatesConfig()


def test_open_position_flags_fee_hurdle_above_and_below_threshold(tmp_path):
    manager, _, _, strategy = _manager(tmp_path)
    above = manager.open_position(_candidate(structure_stop=98.0), 1_000.0, strategy)
    expected_above = fee_hurdle_r(
        above.entry_price,
        above.qty,
        above.initial_risk_per_unit,
        strategy.assumed_fee_pct_per_side,
    )
    assert expected_above is not None and expected_above > 0.5
    assert above.status == TradeStatus.OPEN
    assert above.exit_snapshot["shadow_gate_fee_hurdle_r_max"] == pytest.approx(0.5)
    assert above.exit_snapshot["shadow_block_fee_hurdle"] is True
    assert above.exit_snapshot["shadow_block_cross_sleeve"] is False
    assert above.exit_snapshot["shadow_cross_sleeve_holders"] == []
    assert above.exit_snapshot["shadow_gates_version"] == 1
    assert above.exit_snapshot["fee_hurdle_r"] == round(expected_above, 6)

    manager_below, _, _, strategy_below = _manager(tmp_path / "below")
    below = manager_below.open_position(_candidate(structure_stop=90.0), 1_000.0, strategy_below)
    expected_below = fee_hurdle_r(
        below.entry_price,
        below.qty,
        below.initial_risk_per_unit,
        strategy_below.assumed_fee_pct_per_side,
    )
    assert expected_below is not None and expected_below < 0.5
    assert below.exit_snapshot["shadow_block_fee_hurdle"] is False
    assert below.exit_snapshot["shadow_gates_version"] == 1


def test_open_position_flags_cross_sleeve_holder_present_and_absent(tmp_path):
    strategy = make_strategy(name="intraday")
    manager, _, _, _ = _manager(tmp_path, strategy=strategy)
    absent = manager.open_position(_candidate(strategy=strategy.name), 1_000.0, strategy)
    assert absent.exit_snapshot["shadow_block_cross_sleeve"] is False
    assert absent.exit_snapshot["shadow_cross_sleeve_holders"] == []

    manager_held, _, _, _ = _manager(
        tmp_path / "held",
        strategy=strategy,
        extra_open=[
            _open_holder(strategy="swing"),
            _open_holder(strategy="bear_rally"),
            _open_holder(ticker="SOL", strategy="swing"),
            _open_holder(strategy="intraday"),
        ],
    )
    held = manager_held.open_position(_candidate(strategy=strategy.name), 1_000.0, strategy)
    assert held.exit_snapshot["shadow_block_cross_sleeve"] is True
    assert held.exit_snapshot["shadow_cross_sleeve_holders"] == ["bear_rally", "swing"]


def test_open_position_none_hurdle_writes_null_fee_block(tmp_path):
    class ZeroQtyBroker:
        name = "paper"
        server_side_brackets = False
        offline_simulation = True

        def current_price(self, _product):
            return 100.0

        def open_long(self, _product, _notional, _tp, _sl):
            return Fill("buy", 100.0, 0.0, 0.0)

    store = make_store(tmp_path)
    strategy = make_strategy()
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        ZeroQtyBroker(),
        strategies=[strategy],
    )
    trade = manager.open_position(_candidate(strategy=strategy.name), 1_000.0, strategy)
    assert trade.status == TradeStatus.OPEN
    assert trade.exit_snapshot["shadow_block_fee_hurdle"] is None
    assert trade.exit_snapshot["shadow_gates_version"] == 1
    assert "fee_hurdle_r" not in trade.exit_snapshot


def test_order_flow_unchanged_when_fee_hurdle_gate_is_flagged(tmp_path):
    strategy = make_strategy()
    candidate = _candidate(structure_stop=98.0)

    def _run(path, threshold: float):
        store = make_store(path)
        broker = PaperBroker(seed=11)
        broker.set_price("BTC-USD", 100.0)
        calls: list[tuple] = []
        original = broker.open_long

        def _open_long(product_id, notional_usd, tp, sl):
            calls.append((product_id, notional_usd, tp, sl))
            return original(product_id, notional_usd, tp, sl)

        broker.open_long = _open_long
        manager = TradeManager(
            Settings(paper_start_equity=5_000),
            make_universe(),
            store,
            broker,
            strategies=[strategy],
            shadow_gate_fee_hurdle_r_max=threshold,
        )
        trade = manager.open_position(candidate, 1_000.0, strategy)
        return trade, calls

    flagged, flagged_calls = _run(tmp_path / "flagged", 0.5)
    control, control_calls = _run(tmp_path / "control", 999.0)

    assert flagged.exit_snapshot["shadow_block_fee_hurdle"] is True
    assert control.exit_snapshot["shadow_block_fee_hurdle"] is False
    assert flagged_calls == control_calls
    assert len(flagged_calls) == 1
    assert flagged.status == control.status == TradeStatus.OPEN
    assert flagged.qty == pytest.approx(control.qty)
    assert flagged.take_profit == pytest.approx(control.take_profit)
    assert flagged.stop_loss == pytest.approx(control.stop_loss)
    assert flagged.entry_price == pytest.approx(control.entry_price)


def test_shadow_gate_failure_does_not_block_entry(tmp_path, caplog):
    manager, _, store, strategy = _manager(tmp_path)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("store unavailable")

    store.open_trades = _boom
    with caplog.at_level("WARNING"):
        trade = manager.open_position(_candidate(strategy=strategy.name), 1_000.0, strategy)
    assert trade.status == TradeStatus.OPEN
    assert "shadow_gates_version" not in (trade.exit_snapshot or {})
    assert "shadow entry gates failed" in caplog.text


def _closed(
    *,
    ticker: str,
    pnl: float,
    snapshot: dict | None,
    risk: float = 2.0,
    fingerprint: str = "",
    closed_at: datetime | None = None,
) -> Trade:
    now = closed_at or datetime(2026, 8, 15, 16, tzinfo=UTC)
    return Trade(
        ticker=ticker,
        strategy="intraday",
        product_id=f"{ticker}-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=250.0,
        take_profit=110.0,
        stop_loss=98.0,
        initial_risk_per_unit=risk,
        time_stop_at=now,
        realized_pnl=pnl,
        fees_paid=1.0,
        exit_snapshot=snapshot,
        config_fingerprint=fingerprint,
        opened_at=now,
        closed_at=now,
        status=TradeStatus.CLOSED,
    )


def _shadow_snap(*, fee: bool | None, cross: bool, holders: list[str] | None = None) -> dict:
    return {
        "shadow_gate_fee_hurdle_r_max": 0.5,
        "shadow_block_fee_hurdle": fee,
        "shadow_block_cross_sleeve": cross,
        "shadow_cross_sleeve_holders": holders or [],
        "shadow_gates_version": 1,
    }


def test_shadow_gate_summary_math_and_formatter():
    fee_only = _closed(ticker="SOL", pnl=-4.0, snapshot=_shadow_snap(fee=True, cross=False))
    cross_only = _closed(ticker="ETH", pnl=6.0, snapshot=_shadow_snap(fee=False, cross=True))
    neither = _closed(ticker="XRP", pnl=2.0, snapshot=_shadow_snap(fee=False, cross=False))
    none_hurdle = _closed(ticker="ADA", pnl=4.0, snapshot=_shadow_snap(fee=None, cross=False))
    missing = _closed(ticker="DOGE", pnl=100.0, snapshot=None)

    summary = shadow_gate_summary([fee_only, cross_only, neither, none_hurdle, missing])
    assert summary.coverage_n == 4
    assert summary.no_shadow_data == 1

    assert summary.fee_hurdle.flagged.n == 1
    assert summary.fee_hurdle.flagged.net_pnl == pytest.approx(-4.0)
    assert summary.fee_hurdle.flagged.mean_net_r == pytest.approx(-2.0)
    assert summary.fee_hurdle.not_flagged.n == 3
    assert summary.fee_hurdle.not_flagged.net_pnl == pytest.approx(12.0)
    assert summary.fee_hurdle.not_flagged.mean_net_r == pytest.approx(2.0)

    assert summary.cross_sleeve.flagged.n == 1
    assert summary.cross_sleeve.flagged.net_pnl == pytest.approx(6.0)
    assert summary.cross_sleeve.flagged.mean_net_r == pytest.approx(3.0)
    assert summary.cross_sleeve.not_flagged.n == 3
    assert summary.cross_sleeve.not_flagged.net_pnl == pytest.approx(2.0)
    assert summary.cross_sleeve.not_flagged.mean_net_r == pytest.approx(1.0 / 3.0)

    assert summary.either.flagged.n == 2
    assert summary.either.flagged.net_pnl == pytest.approx(2.0)
    assert summary.either.flagged.mean_net_r == pytest.approx(0.5)
    assert summary.either.not_flagged.n == 2
    assert summary.either.not_flagged.net_pnl == pytest.approx(6.0)
    assert summary.either.not_flagged.mean_net_r == pytest.approx(1.5)

    lines = format_shadow_gate_summary(summary, label="this week")
    assert lines[0] == "  this week: coverage n=4  no shadow data: 1"
    assert (
        format_shadow_gate_split("fee-hurdle", summary.fee_hurdle)
        == "    fee-hurdle   flagged n=1 net $-4.00 mean R=-2.00  |  "
        "not-flagged n=3 net $+12.00 mean R=+2.00"
    )


def test_weekly_report_includes_shadow_entry_gates_section(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    gen8 = GEN8_CONFIG_FINGERPRINT_PREFIX + "shadow"

    def add(*, ticker, pnl, snapshot, fingerprint=gen8, closed_at):
        store.add_trade(
            _closed(
                ticker=ticker,
                pnl=pnl,
                snapshot=snapshot,
                fingerprint=fingerprint,
                closed_at=closed_at,
            )
        )

    add(
        ticker="SOL",
        pnl=-4.0,
        snapshot=_shadow_snap(fee=True, cross=False),
        closed_at=end.replace(day=15, hour=16),
    )
    add(
        ticker="ETH",
        pnl=6.0,
        snapshot=_shadow_snap(fee=False, cross=True),
        closed_at=end.replace(day=15, hour=17),
    )
    add(
        ticker="DOGE",
        pnl=1.0,
        snapshot=None,
        closed_at=end.replace(day=15, hour=18),
    )
    add(
        ticker="XRP",
        pnl=8.0,
        snapshot=_shadow_snap(fee=False, cross=False),
        fingerprint="deadbeef",
        closed_at=end.replace(day=15, hour=12),
    )
    add(
        ticker="ADA",
        pnl=-2.0,
        snapshot=_shadow_snap(fee=True, cross=True),
        closed_at=start.replace(hour=10),
    )

    _, body = build_weekly_report(store, ["intraday"], start, end, UTC)
    assert "Shadow entry gates (forward-only, no behavior change; threshold 0.50 R)" in body
    assert "  this week: coverage n=3  no shadow data: 1" in body
    assert (
        f"  since gen-8 (fp {GEN8_CONFIG_FINGERPRINT_PREFIX}): coverage n=3  no shadow data: 1"
        in body
    )
