"""Retrospective read-only replays: fee sensitivity, I4 gates, setup x strategy."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from _helpers import make_store

from smt.cli import build_parser
from smt.config import Settings
from smt.models import ExitReason, Trade, TradeStatus
from smt.ops.replays import (
    COMPARE_SETUPS,
    DEFAULT_FEE_RATES,
    DEFAULT_FEE_RATES_TEXT,
    FEE_SENSITIVITY_HEADER,
    GATE_CROSS_SLEEVE,
    GATE_FEE_HURDLE,
    GATE_UNION,
    HURDLE_RECOMPUTED,
    HURDLE_STORED,
    HURDLE_UNAVAILABLE,
    RETRO_GATES_HEADER,
    SETUP_BY_STRATEGY_HEADER,
    break_even_fee_rate,
    build_fee_sensitivity,
    build_retro_gates,
    build_setup_by_strategy,
    format_replays,
    resolve_fee_hurdle,
    run_replays,
    trade_qty_for_hurdle,
    would_block_cross_sleeve,
    would_block_fee_hurdle,
)
from smt.ops.reports import (
    GEN8_CONFIG_FINGERPRINT_PREFIX,
    SMALL_N_THRESHOLD,
    net_at_fee_rate,
    trade_fee_legs_notional,
    trade_gross_pnl,
    trade_net_at_fee_rate,
    trade_realized_r,
)
from smt.store import OPPORTUNITY_LEDGER_VERSION, opportunity_key
from smt.trader.exit_policy import fee_hurdle_r

GEN8_FP = GEN8_CONFIG_FINGERPRINT_PREFIX + "a" * (64 - len(GEN8_CONFIG_FINGERPRINT_PREFIX))
GEN7_FP = "e788820ab5d0" + "b" * (64 - 12)
T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _trade(**overrides) -> Trade:
    trade_id = overrides.pop("id", None)
    closed = overrides.pop("closed_at", T0 + timedelta(hours=3))
    opened = overrides.pop("opened_at", T0)
    status = overrides.pop("status", TradeStatus.CLOSED)
    pnl = overrides.pop("realized_pnl", 8.0)
    fees = overrides.pop("fees_paid", 2.0)
    values = dict(
        ticker="SOL",
        strategy="intraday",
        product_id="SOL-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=100.0,
        take_profit=110.0,
        stop_loss=90.0,
        highest_price=110.0,
        initial_risk_per_unit=10.0,
        time_stop_at=closed or opened + timedelta(hours=3),
        exit_price=110.0,
        exit_reason=ExitReason.TAKE_PROFIT if pnl >= 0 else ExitReason.STOP_LOSS,
        realized_pnl=pnl,
        fees_paid=fees,
        setup="breakout_close",
        status=status,
        config_fingerprint=GEN8_FP,
        opened_at=opened,
        closed_at=None if status == TradeStatus.OPEN else closed,
    )
    values.update(overrides)
    trade = Trade(**values)
    if trade_id is not None:
        trade.id = trade_id
    return trade


def _persist(store, **overrides) -> Trade:
    return store.add_trade(_trade(**overrides))


def _link_setup(store, trade: Trade, setup_name: str) -> None:
    trigger_ts = 1_700_000_000 + int(trade.id)
    key = opportunity_key(
        config_fingerprint=GEN8_FP,
        run_id="replays-test",
        strategy=trade.strategy,
        ticker=trade.ticker,
        trigger_candle_ts=trigger_ts,
    )
    store.upsert_opportunity(
        opportunity_key=key,
        ledger_version=OPPORTUNITY_LEDGER_VERSION,
        config_fingerprint=GEN8_FP,
        run_id="replays-test",
        strategy=trade.strategy,
        ticker=trade.ticker,
        product_id=trade.product_id,
        trigger_granularity_seconds=900,
        trigger_candle_ts=trigger_ts,
        trigger_closed_at=datetime.fromtimestamp(trigger_ts, tz=UTC),
        outcome_status="opened",
        outcome_reason="filled",
        setup_name=setup_name,
    )
    store.enrich_opportunity(key, trade_id=trade.id)


def _row(table, *, group: str, label: str):
    for row in table.rows:
        if row.group == group and row.label == label:
            return row
    raise AssertionError(f"missing {group}/{label} in {table.rows!r}")


def _gate(table, name: str):
    for row in table.rows:
        if row.name == name:
            return row
    raise AssertionError(f"missing gate {name}")


def test_break_even_fee_rate_is_gross_over_legs():
    trade = _trade(
        original_qty=5.0,
        qty=5.0,
        entry_price=100.0,
        exit_price=100.0,
        realized_pnl=6.0,
        fees_paid=4.0,
    )
    assert trade_gross_pnl(trade) == pytest.approx(10.0)
    assert trade_fee_legs_notional(trade) == pytest.approx(1000.0)
    assert break_even_fee_rate([trade]) == pytest.approx(0.01)

    loser = _trade(realized_pnl=-6.0, fees_paid=4.0)
    assert trade_gross_pnl(loser) <= 0
    assert break_even_fee_rate([loser]) is None
    assert break_even_fee_rate([_trade(realized_pnl=-2.0, fees_paid=2.0)]) is None


def test_fee_sensitivity_all_setup_strategy_and_what_if_nets():
    a = _trade(
        id=1,
        setup="breakout_retest",
        strategy="intraday",
        realized_pnl=8.0,
        fees_paid=2.0,
        original_qty=1.0,
        qty=1.0,
        entry_price=100.0,
        exit_price=110.0,
    )
    b = _trade(
        id=2,
        ticker="BTC",
        product_id="BTC-USD",
        setup="breakout_close",
        strategy="swing",
        realized_pnl=-5.0,
        fees_paid=1.0,
        original_qty=2.0,
        qty=1.0,
        entry_price=50.0,
        take_profit=60.0,
        exit_price=40.0,
        partial_taken=True,
        initial_risk_per_unit=10.0,
    )
    rates = DEFAULT_FEE_RATES
    table = build_fee_sensitivity([a, b], {}, rates)
    all_row = _row(table, group="all", label="All")
    assert all_row.n == 2
    assert all_row.gross == pytest.approx(trade_gross_pnl(a) + trade_gross_pnl(b))
    assert all_row.actual_fees == pytest.approx(3.0)
    assert all_row.actual_net == pytest.approx(3.0)
    for i, rate in enumerate(rates):
        assert all_row.fees_at_rate[i] == pytest.approx(
            trade_fee_legs_notional(a) * rate + trade_fee_legs_notional(b) * rate
        )
        assert all_row.net_at_rate[i] == pytest.approx(net_at_fee_rate([a, b], rate))
        assert all_row.net_at_rate[i] == pytest.approx(
            trade_net_at_fee_rate(a, rate) + trade_net_at_fee_rate(b, rate)
        )
    assert _row(table, group="setup", label="breakout_retest").n == 1
    assert _row(table, group="setup", label="breakout_close").n == 1
    assert _row(table, group="strategy", label="intraday").n == 1
    assert _row(table, group="strategy", label="swing").n == 1
    assert table.break_even_fee_rate == pytest.approx(break_even_fee_rate([a, b]))


def test_fee_sensitivity_uses_linked_setup_name():
    trade = _trade(id=5, setup="breakout_close")
    table = build_fee_sensitivity([trade], {5: "breakout_retest"}, DEFAULT_FEE_RATES)
    assert _row(table, group="setup", label="breakout_retest").n == 1
    assert _row(table, group="setup", label="breakout_close").n == 0


def test_resolve_fee_hurdle_stored_recomputed_unavailable():
    stored = _trade(exit_snapshot={"fee_hurdle_r": 0.4, "fee_hurdle_pct_per_side": 0.006})
    hurdle, source = resolve_fee_hurdle(stored)
    assert source == HURDLE_STORED
    assert hurdle == pytest.approx(0.4)

    missing = _trade(
        exit_snapshot=None,
        entry_price=100.0,
        original_qty=2.0,
        qty=2.0,
        initial_risk_per_unit=2.0,
    )
    expected = fee_hurdle_r(100.0, 2.0, 2.0, 0.006)
    hurdle, source = resolve_fee_hurdle(
        missing, strategy_fee_rates={"intraday": 0.006}, default_fee_pct_per_side=0.006
    )
    assert source == HURDLE_RECOMPUTED
    assert hurdle == pytest.approx(expected)
    assert expected == pytest.approx(0.6)
    assert trade_qty_for_hurdle(missing) == pytest.approx(2.0)

    legacy_qty = _trade(
        exit_snapshot=None,
        entry_price=100.0,
        original_qty=0.0,
        qty=2.0,
        initial_risk_per_unit=2.0,
    )
    assert trade_qty_for_hurdle(legacy_qty) == pytest.approx(2.0)
    hurdle, source = resolve_fee_hurdle(
        legacy_qty, strategy_fee_rates={"intraday": 0.006}, default_fee_pct_per_side=0.006
    )
    assert source == HURDLE_RECOMPUTED
    assert hurdle == pytest.approx(0.6)

    snapshot_pct = _trade(
        exit_snapshot={"fee_hurdle_pct_per_side": 0.01},
        entry_price=100.0,
        original_qty=2.0,
        qty=2.0,
        initial_risk_per_unit=2.0,
    )
    hurdle, source = resolve_fee_hurdle(snapshot_pct, strategy_fee_rates={"intraday": 0.006})
    assert source == HURDLE_RECOMPUTED
    assert hurdle == pytest.approx(fee_hurdle_r(100.0, 2.0, 2.0, 0.01))

    unknown = _trade(
        strategy="not_a_sleeve",
        exit_snapshot={},
        entry_price=100.0,
        original_qty=2.0,
        qty=2.0,
        initial_risk_per_unit=2.0,
    )
    hurdle, source = resolve_fee_hurdle(unknown, strategy_fee_rates={"intraday": 0.004})
    assert source == HURDLE_RECOMPUTED
    assert hurdle == pytest.approx(fee_hurdle_r(100.0, 2.0, 2.0, 0.006))

    dead = _trade(exit_snapshot=None, initial_risk_per_unit=0.0, qty=1.0, original_qty=1.0)
    hurdle, source = resolve_fee_hurdle(dead)
    assert source == HURDLE_UNAVAILABLE
    assert hurdle is None


def test_fee_hurdle_gate_threshold_and_equal_is_kept():
    assert would_block_fee_hurdle(0.6, 0.5) is True
    assert would_block_fee_hurdle(0.5, 0.5) is False
    assert would_block_fee_hurdle(0.4, 0.5) is False
    assert would_block_fee_hurdle(None, 0.5) is False


def test_cross_sleeve_overlap_pair_and_non_matches():
    t1 = T0 + timedelta(hours=1)
    t2 = T0 + timedelta(hours=4)
    holder = _trade(
        id=10,
        ticker="SOL",
        strategy="swing",
        opened_at=T0,
        closed_at=t2,
        config_fingerprint=GEN7_FP,
    )
    subject = _trade(
        id=11,
        ticker="SOL",
        strategy="intraday",
        opened_at=t1,
        closed_at=t2,
        config_fingerprint=GEN8_FP,
    )
    assert would_block_cross_sleeve(subject, [holder, subject]) is True

    same_sleeve = _trade(id=12, ticker="SOL", strategy="intraday", opened_at=T0, closed_at=t2)
    assert would_block_cross_sleeve(subject, [same_sleeve]) is False

    other_ticker = _trade(id=13, ticker="BTC", strategy="swing", opened_at=T0, closed_at=t2)
    assert would_block_cross_sleeve(subject, [other_ticker]) is False

    closed_before = _trade(id=14, ticker="SOL", strategy="swing", opened_at=T0, closed_at=t1)
    assert would_block_cross_sleeve(subject, [closed_before]) is False

    later = _trade(id=15, ticker="SOL", strategy="swing", opened_at=t1 + timedelta(minutes=1))
    assert would_block_cross_sleeve(subject, [later]) is False

    still_open = _trade(
        id=16,
        ticker="SOL",
        strategy="swing",
        opened_at=T0,
        status=TradeStatus.OPEN,
        closed_at=None,
        config_fingerprint=GEN7_FP,
    )
    assert would_block_cross_sleeve(subject, [still_open]) is True


def test_retro_gates_coverage_union_and_mean_r():
    stored_block = _trade(
        id=1,
        ticker="HYPE",
        product_id="HYPE-USD",
        realized_pnl=-4.0,
        fees_paid=1.0,
        initial_risk_per_unit=2.0,
        original_qty=1.0,
        qty=1.0,
        exit_snapshot={"fee_hurdle_r": 0.6},
        opened_at=T0 + timedelta(hours=2),
        closed_at=T0 + timedelta(hours=5),
    )
    recomputed_keep = _trade(
        id=2,
        ticker="ETH",
        product_id="ETH-USD",
        realized_pnl=6.0,
        fees_paid=1.0,
        initial_risk_per_unit=10.0,
        original_qty=2.0,
        qty=2.0,
        entry_price=100.0,
        exit_snapshot=None,
        opened_at=T0 + timedelta(hours=2),
        closed_at=T0 + timedelta(hours=5),
    )
    unavailable = _trade(
        id=3,
        ticker="BTC",
        product_id="BTC-USD",
        realized_pnl=2.0,
        fees_paid=1.0,
        initial_risk_per_unit=0.0,
        original_qty=1.0,
        qty=1.0,
        exit_snapshot=None,
        opened_at=T0 + timedelta(hours=2),
        closed_at=T0 + timedelta(hours=5),
    )
    cross_only = _trade(
        id=4,
        ticker="SOL",
        strategy="intraday",
        realized_pnl=-2.0,
        fees_paid=1.0,
        initial_risk_per_unit=10.0,
        original_qty=1.0,
        qty=1.0,
        exit_snapshot={"fee_hurdle_r": 0.2},
        opened_at=T0 + timedelta(hours=1),
        closed_at=T0 + timedelta(hours=5),
    )
    holder = _trade(
        id=99,
        ticker="SOL",
        strategy="swing",
        opened_at=T0,
        closed_at=T0 + timedelta(hours=6),
        config_fingerprint=GEN7_FP,
        realized_pnl=0.0,
    )
    cohort = [stored_block, recomputed_keep, unavailable, cross_only]
    table = build_retro_gates(
        cohort,
        [holder, *cohort],
        fee_hurdle_r_max=0.5,
        strategy_fee_rates={"intraday": 0.006},
    )
    assert table.coverage.stored == 2
    assert table.coverage.recomputed == 1
    assert table.coverage.unavailable == 1
    assert table.coverage.n == 4

    fee = _gate(table, GATE_FEE_HURDLE)
    assert fee.blocked_n == 1
    assert fee.blocked_net == pytest.approx(-4.0)
    assert fee.blocked_mean_r == pytest.approx(trade_realized_r(stored_block))
    assert fee.kept_n == 3
    assert fee.total == 4

    cross = _gate(table, GATE_CROSS_SLEEVE)
    assert cross.blocked_n == 1
    assert cross.blocked_net == pytest.approx(-2.0)
    assert cross.kept_n == 3

    union = _gate(table, GATE_UNION)
    assert union.blocked_n == 2
    assert union.blocked_net == pytest.approx(-6.0)
    assert union.kept_n == 2
    assert union.total == 4


def test_setup_by_strategy_small_n_and_excludes_other_setups():
    retest = _trade(
        id=1,
        setup="breakout_retest",
        strategy="intraday",
        realized_pnl=5.0,
        fees_paid=1.0,
        initial_risk_per_unit=2.0,
        original_qty=1.0,
        qty=1.0,
    )
    close = _trade(
        id=2,
        setup="breakout_close",
        strategy="intraday",
        realized_pnl=-1.0,
        fees_paid=1.0,
        initial_risk_per_unit=2.0,
        original_qty=1.0,
        qty=1.0,
    )
    vwap = _trade(id=3, setup="vwap_pullback", strategy="swing", realized_pnl=9.0)
    table = build_setup_by_strategy([retest, close, vwap], {})
    labels = {(row.setup, row.strategy) for row in table.rows}
    assert labels == {
        ("breakout_retest", "intraday"),
        ("breakout_close", "intraday"),
    }
    assert COMPARE_SETUPS == ("breakout_retest", "breakout_close")
    retest_row = next(row for row in table.rows if row.setup == "breakout_retest")
    assert retest_row.n == 1
    assert retest_row.gross == pytest.approx(trade_gross_pnl(retest))
    assert retest_row.fees == pytest.approx(1.0)
    assert retest_row.net == pytest.approx(5.0)
    assert retest_row.mean_net_r == pytest.approx(trade_realized_r(retest))
    assert retest_row.net_win_pct == pytest.approx(1.0)
    assert retest_row.small_n is True
    assert retest_row.n < SMALL_N_THRESHOLD
    close_row = next(row for row in table.rows if row.setup == "breakout_close")
    assert close_row.net_win_pct == pytest.approx(0.0)
    assert close_row.small_n is True


def test_setup_by_strategy_format_tags_small_n():
    from smt.ops.replays import format_setup_by_strategy

    table = build_setup_by_strategy(
        [_trade(id=1, setup="breakout_retest", strategy="intraday")], {}
    )
    text = format_setup_by_strategy(table)
    assert SETUP_BY_STRATEGY_HEADER in text
    assert "[small-n]" in text
    assert "breakout_retest" in text
    assert "intraday" in text


def test_run_replays_filters_fingerprint_uses_all_holders_and_linked_setups(tmp_path):
    store = make_store(tmp_path)
    gen8 = _persist(
        store,
        setup="breakout_close",
        strategy="intraday",
        realized_pnl=8.0,
        fees_paid=2.0,
        original_qty=5.0,
        qty=5.0,
        entry_price=100.0,
        exit_price=100.0,
        initial_risk_per_unit=10.0,
        opened_at=T0 + timedelta(hours=1),
        closed_at=T0 + timedelta(hours=4),
        exit_snapshot={"fee_hurdle_r": 0.2},
    )
    _link_setup(store, gen8, "breakout_retest")
    _persist(
        store,
        ticker="SOL",
        strategy="swing",
        config_fingerprint=GEN7_FP,
        realized_pnl=99.0,
        fees_paid=50.0,
        opened_at=T0,
        closed_at=T0 + timedelta(hours=5),
    )
    _persist(
        store,
        ticker="ETH",
        product_id="ETH-USD",
        strategy="intraday",
        config_fingerprint=GEN7_FP,
        realized_pnl=1.0,
        setup="breakout_close",
    )
    still_open = _persist(
        store,
        ticker="BTC",
        product_id="BTC-USD",
        strategy="swing",
        status=TradeStatus.OPEN,
        closed_at=None,
        opened_at=T0,
        config_fingerprint=GEN7_FP,
    )
    assert still_open.status == TradeStatus.OPEN

    result = run_replays(store, fee_hurdle_r_max=0.5, strategy_fee_rates={"intraday": 0.006})
    assert result.n == 1
    assert result.fingerprint_prefix == GEN8_CONFIG_FINGERPRINT_PREFIX
    all_row = _row(result.fee_sensitivity, group="all", label="All")
    assert all_row.n == 1
    assert all_row.actual_net == pytest.approx(8.0)
    assert _row(result.fee_sensitivity, group="setup", label="breakout_retest").n == 1
    assert result.fee_sensitivity.break_even_fee_rate == pytest.approx(0.01)

    cross = _gate(result.retro_gates, GATE_CROSS_SLEEVE)
    assert cross.blocked_n == 1
    assert result.retro_gates.coverage.stored == 1
    setup_row = result.setup_by_strategy.rows[0]
    assert setup_row.setup == "breakout_retest"
    assert setup_row.strategy == "intraday"

    text = format_replays(result)
    assert FEE_SENSITIVITY_HEADER in text
    assert RETRO_GATES_HEADER in text
    assert SETUP_BY_STRATEGY_HEADER in text
    assert "Break-even fee rate per side (All): 1.00%" in text


def test_run_replays_missing_hurdle_is_recomputed(tmp_path):
    store = make_store(tmp_path)
    _persist(
        store,
        exit_snapshot=None,
        entry_price=100.0,
        original_qty=1.0,
        qty=1.0,
        initial_risk_per_unit=2.0,
        realized_pnl=-4.0,
        fees_paid=1.0,
    )
    result = run_replays(store, strategy_fee_rates={"intraday": 0.006})
    assert result.retro_gates.coverage.recomputed == 1
    assert result.retro_gates.coverage.stored == 0
    fee = _gate(result.retro_gates, GATE_FEE_HURDLE)
    assert fee.blocked_n == 1
    assert fee.blocked_mean_r == pytest.approx(-2.0)


def test_cli_replays_defaults_and_flags():
    parser = build_parser()
    default = parser.parse_args(["replays"])
    assert default.fingerprint_prefix == GEN8_CONFIG_FINGERPRINT_PREFIX
    assert default.fee_rates == DEFAULT_FEE_RATES
    assert tuple(default.fee_rates) == tuple(float(x) for x in DEFAULT_FEE_RATES_TEXT.split(","))
    assert default.json is False

    args = parser.parse_args(
        ["replays", "--fingerprint-prefix", "abc", "--fee-rates", "0.004,0.009", "--json"]
    )
    assert args.fingerprint_prefix == "abc"
    assert args.fee_rates == (0.004, 0.009)
    assert args.json is True

    with pytest.raises(SystemExit):
        parser.parse_args(["replays", "--fee-rates", "-0.001"])
    with pytest.raises(SystemExit):
        parser.parse_args(["replays", "--fee-rates", ""])


def test_cli_replays_smoke_against_temp_sqlite(tmp_path, monkeypatch, capsys):
    store = make_store(tmp_path)
    _persist(
        store,
        setup="breakout_retest",
        realized_pnl=3.0,
        fees_paid=1.0,
        original_qty=1.0,
        qty=1.0,
        entry_price=100.0,
        exit_price=110.0,
        initial_risk_per_unit=10.0,
        exit_snapshot={"fee_hurdle_r": 0.2},
    )
    monkeypatch.setattr(
        "smt.config.get_settings",
        lambda: Settings(database_url=store.database_url),
    )
    parser = build_parser()
    args = parser.parse_args(["replays"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert FEE_SENSITIVITY_HEADER in out
    assert RETRO_GATES_HEADER in out
    assert SETUP_BY_STRATEGY_HEADER in out

    json_args = parser.parse_args(["replays", "--json"])
    assert json_args.func(json_args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n"] == 1
    assert payload["fingerprint_prefix"] == GEN8_CONFIG_FINGERPRINT_PREFIX
    assert "fee_sensitivity" in payload
    assert "retro_gates" in payload
    assert "setup_by_strategy" in payload
    assert payload["fee_sensitivity"]["fee_rates"] == list(DEFAULT_FEE_RATES)
    assert payload["retro_gates"]["coverage"]["n"] == 1
