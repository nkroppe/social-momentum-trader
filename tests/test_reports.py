"""Weekly scheduling, report formatting, and trade notifications."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from _helpers import make_store

from smt.config import TradeAlertsConfig, WeeklyReportConfig
from smt.models import ExitReason, Trade, TradeStatus
from smt.ops.alerts import split_message
from smt.ops.reports import (
    GEN8_CONFIG_FINGERPRINT_PREFIX,
    MFE_R_EPSILON,
    NOTIONAL_BUCKETS,
    SETUP_BUCKETS,
    UNKNOWN_SETUP,
    aggregate_cost_stats,
    aggregate_mfe_capture,
    aggregate_stop_loss_fills,
    build_compare_report,
    build_weekly_report,
    classify_notional,
    classify_setup,
    format_stop_loss_fill_rollup_row,
    format_stop_loss_week_summary,
    intraday_notional_fee_rows,
    is_hard_stop_loss,
    notional_cost_stats,
    resolve_setup_name,
    setup_cost_stats,
    stop_loss_dollar_slip,
    stop_loss_fill_rollup,
    stop_loss_fills,
    stop_loss_r_slip,
    ticker_cost_stats,
    trade_closed_alert,
    trade_fee_pct_of_notional,
    trade_gross_pnl,
    trade_mfe_capture,
    trade_opened_alert,
    trade_realized_r,
    trades_matching_config_fingerprint,
)
from smt.ops.schedule import WeeklyScheduler
from smt.store import OPPORTUNITY_LEDGER_VERSION, opportunity_key

EASTERN = ZoneInfo("America/New_York")


def _cfg(tmp_path, **overrides) -> WeeklyReportConfig:
    return WeeklyReportConfig(state_file=str(tmp_path / "weekly.json"), **overrides)


def _at(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=EASTERN)


# ---- Scheduling -------------------------------------------------------------


def test_previous_occurrence_is_the_last_sunday_8pm(tmp_path):
    s = WeeklyScheduler(_cfg(tmp_path))
    # Wednesday Aug 12, 2026, 10:00 Eastern.
    assert s.previous_occurrence(_at(2026, 8, 12, 10)) == _at(2026, 8, 9, 20)


def test_before_the_hour_on_the_day_itself_looks_back_a_week(tmp_path):
    s = WeeklyScheduler(_cfg(tmp_path))
    # Sunday 19:59 is still the previous week's report window.
    assert s.previous_occurrence(_at(2026, 8, 9, 19, 59)) == _at(2026, 8, 2, 20)
    assert s.previous_occurrence(_at(2026, 8, 9, 20, 0)) == _at(2026, 8, 9, 20)


def test_next_occurrence_holds_the_wall_clock_across_dst(tmp_path):
    """Adding 7*24h would shift the send time by an hour at a DST boundary."""
    s = WeeklyScheduler(_cfg(tmp_path))
    # US DST ends Sunday Nov 1, 2026.
    nxt = s.next_occurrence(_at(2026, 10, 25, 21))
    assert (nxt.year, nxt.month, nxt.day, nxt.hour) == (2026, 11, 1, 20)
    assert nxt.utcoffset() == timedelta(hours=-5)  # EST, not EDT


def test_first_run_does_not_immediately_fire_a_report(tmp_path):
    s = WeeklyScheduler(_cfg(tmp_path))
    now = _at(2026, 8, 12, 10)
    s.ensure_initialized(now)
    assert s.due(now) is None


def test_report_becomes_due_once_the_time_passes(tmp_path):
    s = WeeklyScheduler(_cfg(tmp_path))
    s.ensure_initialized(_at(2026, 8, 12, 10))
    due = s.due(_at(2026, 8, 16, 20, 1))
    assert due == _at(2026, 8, 16, 20)

    s.mark_sent(due)
    assert s.due(_at(2026, 8, 16, 20, 5)) is None


def test_a_send_missed_during_downtime_is_delivered_late(tmp_path):
    """A skipped week would leave a silent hole in the record."""
    s = WeeklyScheduler(_cfg(tmp_path))
    s.mark_sent(_at(2026, 8, 9, 20))
    # Bot was down all Sunday evening and came back Tuesday.
    assert s.due(_at(2026, 8, 18, 9)) == _at(2026, 8, 16, 20)


def test_disabled_schedule_is_never_due(tmp_path):
    s = WeeklyScheduler(_cfg(tmp_path, enabled=False))
    assert s.due(_at(2026, 8, 18, 9)) is None


def test_report_windows_do_not_overlap(tmp_path):
    s = WeeklyScheduler(_cfg(tmp_path))
    first = _at(2026, 8, 9, 20)
    start, end = s.report_window(first)
    assert end == first
    assert start == _at(2026, 8, 2, 20)
    # The next window begins exactly where this one ended.
    assert s.report_window(s.next_occurrence(first))[0] == end


def test_unknown_timezone_falls_back_instead_of_crashing(tmp_path):
    s = WeeklyScheduler(_cfg(tmp_path, timezone="Mars/Olympus_Mons"))
    assert s.tz is UTC
    assert s.previous_occurrence(_at(2026, 8, 12, 10)) is not None


# ---- Report content ---------------------------------------------------------


def _closed_trade(
    store,
    ticker,
    pnl,
    *,
    strategy="intraday",
    closed_at,
    notional=250.0,
    fees=1.0,
    setup="",
    highest_price=0.0,
    initial_risk_per_unit=0.0,
    original_qty=1.0,
    qty=1.0,
    stop_loss=95.0,
    exit_price=None,
    exit_reason=None,
    trailing_stop=0.0,
    config_fingerprint="",
    exit_snapshot=None,
):
    trade = Trade(
        ticker=ticker,
        strategy=strategy,
        product_id=f"{ticker}-USD",
        is_live=False,
        status=TradeStatus.CLOSED,
        qty=qty,
        original_qty=original_qty,
        entry_price=100.0,
        entry_notional=notional,
        take_profit=110.0,
        stop_loss=stop_loss,
        trailing_stop=trailing_stop,
        highest_price=highest_price,
        initial_risk_per_unit=initial_risk_per_unit,
        time_stop_at=closed_at,
        exit_price=100.0 + pnl if exit_price is None else exit_price,
        exit_reason=(
            exit_reason
            if exit_reason is not None
            else (ExitReason.TAKE_PROFIT if pnl >= 0 else ExitReason.STOP_LOSS)
        ),
        realized_pnl=pnl,
        fees_paid=fees,
        setup=setup,
        exit_snapshot=exit_snapshot,
        config_fingerprint=config_fingerprint,
        opened_at=closed_at - timedelta(hours=3),
        closed_at=closed_at,
    )
    return store.add_trade(trade)


def _link_setup(store, trade, setup_name: str, *, run_id: str = "report-test") -> str:
    fingerprint = "a" * 64
    trigger_ts = 1_700_000_000 + int(trade.id)
    key = opportunity_key(
        config_fingerprint=fingerprint,
        run_id=run_id,
        strategy=trade.strategy,
        ticker=trade.ticker,
        trigger_candle_ts=trigger_ts,
    )
    store.upsert_opportunity(
        opportunity_key=key,
        ledger_version=OPPORTUNITY_LEDGER_VERSION,
        config_fingerprint=fingerprint,
        run_id=run_id,
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
    return key


def test_weekly_report_totals_only_the_window(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)

    _closed_trade(store, "SOL", 15.40, closed_at=end - timedelta(days=1))
    _closed_trade(store, "BTC", -8.10, strategy="swing", closed_at=end - timedelta(days=2))
    # Outside the window on both sides.
    _closed_trade(store, "ETH", 999.0, closed_at=start - timedelta(hours=1))
    _closed_trade(store, "HYPE", 999.0, closed_at=end + timedelta(hours=1))

    subject, body = build_weekly_report(store, ["intraday", "swing"], start, end, UTC, mode="PAPER")

    assert "+7.30" in subject
    assert "Trades closed:  2" in body
    assert "NET P/L:        $+7.30" in body
    assert "Gross P/L:      $+9.30" in body
    assert "Fees paid:      $2.00" in body
    assert "Fee% of notional: 0.40%" in body
    assert "50% (1W / 1L) net" in body
    assert "50% (1W / 1L) gross" in body
    assert "By strategy:" in body
    assert "999" not in body
    assert "SOL" in body and "BTC" in body


def test_weekly_report_handles_a_week_with_no_trades(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    subject, body = build_weekly_report(store, ["intraday"], end - timedelta(days=7), end, UTC)
    assert "$+0.00" in subject
    assert "No trades closed this week." in body


def test_weekly_report_caps_the_trade_list(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    for i in range(10):
        _closed_trade(store, "SOL", 1.0, closed_at=end - timedelta(hours=i + 1))

    _, body = build_weekly_report(
        store, ["intraday"], end - timedelta(days=7), end, UTC, max_trades_listed=4
    )
    assert "... and 6 more" in body


def test_weekly_report_reports_gross_fees_net_and_both_win_rates(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(store, "SOL", 9.0, closed_at=end - timedelta(hours=2), fees=1.0, notional=250.0)
    _closed_trade(
        store,
        "BTC",
        -0.50,
        strategy="swing",
        closed_at=end - timedelta(hours=3),
        fees=1.0,
        notional=250.0,
    )

    _, body = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)

    assert "Gross P/L:      $+10.50" in body
    assert "Fees paid:      $2.00" in body
    assert "NET P/L:        $+8.50" in body
    assert "Fee% of notional: 0.40%" in body
    assert "50%" in body and "100%" in body
    assert "net" in body and "gross" in body
    assert "By strategy:" in body


def test_setup_buckets_sum_to_closed_trades_and_use_both_sources(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)

    _closed_trade(store, "SOL", 5.0, closed_at=end - timedelta(hours=1), setup="breakout_retest")
    _closed_trade(store, "BTC", 3.0, closed_at=end - timedelta(hours=2), setup="breakout_close")
    _closed_trade(store, "ETH", 1.0, closed_at=end - timedelta(hours=3), setup="vwap_pullback")
    empty = _closed_trade(store, "ZEC", -1.0, closed_at=end - timedelta(hours=4), setup="")
    linked = _closed_trade(store, "HYPE", 2.0, closed_at=end - timedelta(hours=5), setup="")
    _link_setup(store, linked, "breakout_retest")
    kept = _closed_trade(
        store, "PUMP", 0.5, closed_at=end - timedelta(hours=6), setup="breakout_close"
    )
    _link_setup(store, kept, "")

    _, body = build_weekly_report(store, ["intraday"], start, end, UTC)
    closed = list(store.closed_trades_between(start, end))
    linked_setups = store.setup_names_for_trade_ids(t.id for t in closed if t.id)
    rows = setup_cost_stats(closed, linked_setups)
    counts = {name: stats.n for name, stats in rows}

    assert [name for name, _ in rows] == list(SETUP_BUCKETS)
    assert counts["breakout_retest"] == 2
    # Empty linked setup_name falls back to Trade.setup.
    assert counts["breakout_close"] == 2
    assert counts["vwap"] == 1
    assert counts["unknown"] == 1
    assert sum(counts.values()) == len(closed) == 6
    assert "By setup:" in body
    assert resolve_setup_name(empty, {}) == "unknown"
    assert classify_setup("", "vwap_pullback") == "vwap"
    assert classify_setup("failed_breakdown") == UNKNOWN_SETUP


def test_compare_report_uses_net_headline_and_setup_split(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    _closed_trade(
        store,
        "SOL",
        9.0,
        strategy="intraday",
        closed_at=end,
        fees=1.0,
        notional=200.0,
        setup="breakout_close",
    )
    _closed_trade(
        store,
        "BTC",
        -2.0,
        strategy="swing",
        closed_at=end,
        fees=1.0,
        notional=300.0,
        setup="breakout_retest",
    )

    body = build_compare_report(store, [("intraday", 0.20, 2_000.0), ("swing", 0.60, 6_000.0)])
    assert "NET_WR" in body and "GROSS_WR" in body
    assert "Headline win rate is net" in body
    assert "By setup (closed trades):" in body
    overall = aggregate_cost_stats(list(store.closed_trades()))
    assert overall.net_pnl == 7.0
    assert overall.gross_pnl == 9.0
    assert overall.fees == 2.0
    assert overall.fee_pct_of_notional == pytest.approx(2.0 / 500.0)


def test_weekly_report_counts_breakeven_separately(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(store, "SOL", 10.0, closed_at=end - timedelta(hours=2))
    _closed_trade(store, "BTC", -5.0, closed_at=end - timedelta(hours=3))
    _closed_trade(store, "ETH", 0.0, closed_at=end - timedelta(hours=4))

    _, body = build_weekly_report(store, ["intraday"], start, end, UTC)
    assert "1W / 1L / 1BE" in body
    assert "67% (2W / 1L) gross" in body


def test_gross_pnl_is_realized_plus_fees_and_fee_pct_uses_notional():
    closed_at = datetime(2026, 8, 16, 20, tzinfo=UTC)
    trade = Trade(
        ticker="SOL",
        product_id="SOL-USD",
        qty=1.0,
        entry_price=100.0,
        entry_notional=200.0,
        take_profit=110.0,
        stop_loss=95.0,
        time_stop_at=closed_at,
        realized_pnl=-0.50,
        fees_paid=1.00,
        setup="breakout_close",
        opened_at=closed_at,
        closed_at=closed_at,
    )
    assert trade_gross_pnl(trade) == 0.50
    assert trade_fee_pct_of_notional(trade) == 0.005
    zero = Trade(
        ticker="BTC",
        product_id="BTC-USD",
        qty=1.0,
        entry_price=100.0,
        entry_notional=0.0,
        take_profit=110.0,
        stop_loss=95.0,
        time_stop_at=closed_at,
        realized_pnl=-2.0,
        fees_paid=2.0,
        opened_at=closed_at,
        closed_at=closed_at,
    )
    assert trade_fee_pct_of_notional(zero) == 0.0


def test_resolve_setup_name_uses_opportunity_then_trade_then_unknown():
    closed_at = datetime(2026, 8, 16, 20, tzinfo=UTC)
    trade = Trade(
        ticker="SOL",
        product_id="SOL-USD",
        qty=1.0,
        entry_price=100.0,
        entry_notional=250.0,
        take_profit=110.0,
        stop_loss=95.0,
        time_stop_at=closed_at,
        setup="breakout_close",
        opened_at=closed_at,
        closed_at=closed_at,
    )
    trade.id = 5
    assert classify_setup("vwap_pullback") == "vwap"
    assert classify_setup("") == UNKNOWN_SETUP
    assert classify_setup("failed_breakdown") == UNKNOWN_SETUP
    assert resolve_setup_name(trade, {5: "breakout_retest"}) == "breakout_retest"
    assert resolve_setup_name(trade, {5: ""}) == "breakout_close"
    assert resolve_setup_name(trade, {5: "   "}) == "breakout_close"
    assert resolve_setup_name(trade, {5: "vwap_pullback"}) == "vwap"
    assert resolve_setup_name(trade, {}) == "breakout_close"
    trade.setup = ""
    assert resolve_setup_name(trade, {}) == UNKNOWN_SETUP
    assert resolve_setup_name(trade, {9: "vwap_pullback"}) == UNKNOWN_SETUP


def test_weekly_report_splits_costs_and_setups(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)

    retest = _closed_trade(
        store, "SOL", -0.40, closed_at=end - timedelta(hours=2), fees=1.00, setup="breakout_close"
    )
    close = _closed_trade(
        store,
        "ETH",
        12.00,
        strategy="swing",
        closed_at=end - timedelta(hours=3),
        fees=2.00,
        setup="breakout_close",
    )
    vwap = _closed_trade(
        store, "BTC", 5.00, closed_at=end - timedelta(hours=4), fees=1.00, setup=""
    )
    other = _closed_trade(
        store,
        "DOGE",
        -3.00,
        closed_at=end - timedelta(hours=5),
        notional=0.0,
        fees=1.50,
        setup="failed_breakdown",
    )
    _link_setup(store, retest, "breakout_retest")
    _link_setup(store, close, "breakout_close")
    _link_setup(store, vwap, "vwap_pullback")

    _, body = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)
    assert "By setup:" in body
    assert "breakout_retest" in body
    assert "breakout_close" in body
    assert "vwap" in body
    assert "unknown" in body
    assert "vwap_pullback" not in body
    assert "failed_breakdown" not in body

    closed = list(store.closed_trades_between(start, end))
    linked = store.setup_names_for_trade_ids(t.id for t in closed)
    buckets = setup_cost_stats(closed, linked)
    assert [name for name, _ in buckets] == list(SETUP_BUCKETS)
    assert sum(stats.n for _, stats in buckets) == len(closed) == 4
    by_name = dict(buckets)
    assert by_name["breakout_retest"].n == 1
    assert by_name["breakout_retest"].net_pnl == -0.40
    assert by_name["breakout_retest"].gross_pnl == 0.60
    assert by_name["breakout_retest"].net_wins == 0
    assert by_name["breakout_retest"].gross_wins == 1
    assert by_name["vwap"].n == 1
    assert by_name[UNKNOWN_SETUP].n == 1
    assert by_name[UNKNOWN_SETUP].fee_pct_of_notional == 0.0
    assert other.setup == "failed_breakdown"

    overall = aggregate_cost_stats(closed)
    assert overall.gross_pnl == pytest.approx(overall.net_pnl + overall.fees)
    assert "TOTAL" in body


def test_empty_setups_count_as_unknown(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    blank = _closed_trade(store, "SOL", 1.0, closed_at=end - timedelta(hours=1), setup="")
    spaced = _closed_trade(store, "ETH", -1.0, closed_at=end - timedelta(hours=2), setup="  ")
    _link_setup(store, blank, "")

    _, body = build_weekly_report(store, ["intraday"], start, end, UTC)
    closed = list(store.closed_trades_between(start, end))
    linked = store.setup_names_for_trade_ids(t.id for t in closed)
    buckets = dict(setup_cost_stats(closed, linked))
    assert list(buckets) == list(SETUP_BUCKETS)
    assert sum(stats.n for stats in buckets.values()) == 2
    assert buckets[UNKNOWN_SETUP].n == 2
    assert UNKNOWN_SETUP in body
    assert spaced.setup.strip() == ""


def test_compare_report_costs_and_setup_totals(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    a = _closed_trade(
        store, "SOL", -0.40, closed_at=end, fees=1.00, setup="ignored", notional=100.0
    )
    b = _closed_trade(
        store,
        "ETH",
        8.00,
        strategy="swing",
        closed_at=end,
        fees=2.00,
        setup="breakout_close",
        notional=200.0,
    )
    c = _closed_trade(store, "BTC", 0.0, closed_at=end, fees=1.00, setup="", notional=100.0)
    _link_setup(store, a, "breakout_retest")
    _link_setup(store, b, "breakout_close")

    body = build_compare_report(
        store, [("intraday", 0.4, 4_000.0), ("swing", 0.6, 6_000.0)], mode="PAPER"
    )
    assert "NET_WR" in body and "GROSS_WR" in body and "FEE%" in body
    assert "By setup (closed trades):" in body
    assert "breakout_retest" in body
    assert "breakout_close" in body
    assert UNKNOWN_SETUP in body
    assert "40%" in body and "60%" in body
    assert "Slippage stays in fill price / gross" in body

    closed = list(store.closed_trades())
    linked = store.setup_names_for_trade_ids(t.id for t in closed)
    buckets = setup_cost_stats(closed, linked)
    assert sum(stats.n for _, stats in buckets) == 3
    by_name = dict(buckets)
    assert by_name["breakout_retest"].gross_pnl == pytest.approx(0.60)
    assert by_name["breakout_retest"].net_pnl == pytest.approx(-0.40)
    assert by_name["breakout_close"].n == 1
    assert by_name[UNKNOWN_SETUP].n == 1
    assert c.setup == ""
    assert "TOTAL" in body
    overall = aggregate_cost_stats(closed)
    assert overall.n == 3
    assert overall.net_wins == 1
    assert overall.net_breakeven == 1
    assert overall.gross_wins == 3
    assert overall.fee_pct_of_notional == pytest.approx(4.0 / 400.0)


def test_compare_report_empty_book_has_zero_rates(tmp_path):
    store = make_store(tmp_path)
    body = build_compare_report(store, ["intraday"], mode="PAPER")
    assert "No closed trades." in body
    assert "0%" in body
    assert "By setup" not in body


def test_notional_fee_buckets_boundaries_fee_pct_and_fee_to_gross():
    assert [classify_notional(n) for n in (0.0, 299.99, 300.0, 499.99, 500.0, 699.99, 700.0)] == [
        "<$300",
        "<$300",
        "$300-500",
        "$300-500",
        "$500-700",
        "$500-700",
        ">=$700",
    ]

    closed_at = datetime(2026, 8, 16, 20, tzinfo=UTC)

    def _trade(ticker: str, notional: float, pnl: float, fees: float) -> Trade:
        return Trade(
            ticker=ticker,
            product_id=f"{ticker}-USD",
            qty=1.0,
            entry_price=100.0,
            entry_notional=notional,
            take_profit=110.0,
            stop_loss=95.0,
            time_stop_at=closed_at,
            realized_pnl=pnl,
            fees_paid=fees,
            opened_at=closed_at,
            closed_at=closed_at,
        )

    trades = [
        _trade("SOL", 250.0, 4.0, 1.0),
        _trade("ETH", 300.0, -2.0, 1.5),
        _trade("BTC", 500.0, 6.0, 2.0),
        _trade("HYPE", 700.0, -8.0, 3.5),
    ]
    rows = notional_cost_stats(trades)
    assert [name for name, _ in rows] == list(NOTIONAL_BUCKETS)
    by_name = dict(rows)
    assert sum(stats.n for stats in by_name.values()) == 4
    assert by_name["<$300"].n == 1
    assert by_name["<$300"].fee_pct_of_notional == pytest.approx(1.0 / 250.0)
    assert by_name["<$300"].fee_to_gross == pytest.approx(1.0 / 5.0)
    assert by_name["$300-500"].fee_pct_of_notional == pytest.approx(1.5 / 300.0)
    assert by_name["$300-500"].fee_to_gross == pytest.approx(1.5 / (-2.0 + 1.5))
    assert by_name["$500-700"].fee_to_gross == pytest.approx(2.0 / 8.0)
    assert by_name[">=$700"].fee_pct_of_notional == pytest.approx(3.5 / 700.0)
    assert by_name[">=$700"].fee_to_gross == pytest.approx(3.5 / (-8.0 + 3.5))
    zero_gross = aggregate_cost_stats([_trade("ZEC", 100.0, -1.0, 1.0)])
    assert zero_gross.gross_pnl == 0.0
    assert zero_gross.fee_to_gross == 0.0


def test_closes_by_ticker_groups_closed_trades():
    closed_at = datetime(2026, 8, 16, 20, tzinfo=UTC)

    def _trade(ticker: str, pnl: float, fees: float, notional: float) -> Trade:
        return Trade(
            ticker=ticker,
            product_id=f"{ticker}-USD",
            qty=1.0,
            entry_price=100.0,
            entry_notional=notional,
            take_profit=110.0,
            stop_loss=95.0,
            time_stop_at=closed_at,
            realized_pnl=pnl,
            fees_paid=fees,
            opened_at=closed_at,
            closed_at=closed_at,
        )

    trades = [
        _trade("SOL", 4.0, 1.0, 250.0),
        _trade("BTC", -2.0, 1.0, 400.0),
        _trade("SOL", 1.0, 0.5, 250.0),
    ]
    rows = ticker_cost_stats(trades)
    assert [name for name, _ in rows] == ["BTC", "SOL"]
    by_name = dict(rows)
    assert by_name["SOL"].n == 2
    assert by_name["SOL"].net_pnl == pytest.approx(5.0)
    assert by_name["SOL"].fees == pytest.approx(1.5)
    assert by_name["BTC"].n == 1
    assert by_name["BTC"].fee_pct_of_notional == pytest.approx(1.0 / 400.0)


def test_mfe_capture_excludes_nonpositive_and_does_not_cap_above_one():
    closed_at = datetime(2026, 8, 16, 20, tzinfo=UTC)

    def _trade(*, pnl: float, highest: float, risk: float, qty: float = 1.0) -> Trade:
        return Trade(
            ticker="SOL",
            product_id="SOL-USD",
            qty=qty,
            original_qty=qty,
            entry_price=100.0,
            entry_notional=250.0,
            take_profit=110.0,
            stop_loss=95.0,
            highest_price=highest,
            initial_risk_per_unit=risk,
            time_stop_at=closed_at,
            realized_pnl=pnl,
            fees_paid=0.0,
            opened_at=closed_at,
            closed_at=closed_at,
        )

    # MFE_R = (110-100)/10 = 1.0; realized_R = 15/10 = 1.5 → capture 1.5 (no cap).
    above_one = _trade(pnl=15.0, highest=110.0, risk=10.0)
    assert trade_realized_r(above_one) == pytest.approx(1.5)
    assert trade_mfe_capture(above_one) == pytest.approx(1.5)

    # highest == entry → MFE_R = 0, excluded.
    assert trade_mfe_capture(_trade(pnl=5.0, highest=100.0, risk=10.0)) is None
    # risk <= 0 → MFE_R = 0, excluded.
    assert trade_mfe_capture(_trade(pnl=5.0, highest=120.0, risk=0.0)) is None
    # A representable positive MFE still at/under epsilon is excluded.
    epsilon_high = 100.0 + math.ulp(100.0)
    assert ((epsilon_high - 100.0) / 10.0) <= MFE_R_EPSILON
    assert trade_mfe_capture(_trade(pnl=1.0, highest=epsilon_high, risk=10.0)) is None
    kept = _trade(pnl=-5.0, highest=105.0, risk=10.0)
    assert trade_mfe_capture(kept) == pytest.approx((-5.0 / 10.0) / 0.5)

    stats = aggregate_mfe_capture(
        [
            above_one,
            _trade(pnl=5.0, highest=100.0, risk=10.0),
            kept,
        ]
    )
    assert stats.n == 2
    assert stats.mean == pytest.approx((1.5 + (-1.0)) / 2.0)


def test_weekly_and_compare_wire_notional_ticker_and_mfe_capture(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(
        store,
        "SOL",
        10.0,
        closed_at=end - timedelta(hours=2),
        notional=250.0,
        fees=1.0,
        highest_price=110.0,
        initial_risk_per_unit=10.0,
    )
    _closed_trade(
        store,
        "BTC",
        -5.0,
        strategy="swing",
        closed_at=end - timedelta(hours=3),
        notional=610.0,
        fees=2.0,
        highest_price=105.0,
        initial_risk_per_unit=10.0,
    )

    _, weekly = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)
    assert "By notional:" in weekly
    assert "<$300" in weekly
    assert "$500-700" in weekly
    assert "By ticker:" in weekly
    assert "SOL" in weekly and "BTC" in weekly
    assert "fee/gross" in weekly
    assert "MFE capture (realized_R / MFE_R, exclude MFE_R<=0):" in weekly
    assert "n=2" in weekly

    compare = build_compare_report(store, ["intraday", "swing"])
    assert "By notional:" in compare
    assert "By ticker:" in compare
    assert "MFE capture (realized_R / MFE_R, exclude MFE_R<=0):" in compare
    assert "n=2" in compare


def test_stop_loss_fill_uses_hard_stop_not_trailing_and_existing_risk():
    closed_at = datetime(2026, 8, 16, 20, tzinfo=UTC)
    hard = Trade(
        ticker="SOL",
        product_id="SOL-USD",
        qty=2.0,
        original_qty=2.0,
        entry_price=100.0,
        entry_notional=200.0,
        take_profit=110.0,
        stop_loss=95.0,
        trailing_stop=98.0,
        initial_risk_per_unit=5.0,
        time_stop_at=closed_at,
        exit_price=94.0,
        exit_reason=ExitReason.STOP_LOSS,
        realized_pnl=-12.0,
        fees_paid=1.50,
        opened_at=closed_at,
        closed_at=closed_at,
    )
    assert is_hard_stop_loss(hard) is True
    assert stop_loss_dollar_slip(hard) == pytest.approx(-2.0)
    assert stop_loss_r_slip(hard) == pytest.approx(-0.20)

    trail = Trade(
        ticker="BTC",
        product_id="BTC-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=250.0,
        take_profit=110.0,
        stop_loss=95.0,
        trailing_stop=99.0,
        initial_risk_per_unit=5.0,
        time_stop_at=closed_at,
        exit_price=98.5,
        exit_reason=ExitReason.TRAILING_STOP,
        realized_pnl=-1.5,
        fees_paid=1.0,
        opened_at=closed_at,
        closed_at=closed_at,
    )
    assert is_hard_stop_loss(trail) is False
    rows = stop_loss_fills([hard, trail])
    assert len(rows) == 1
    assert rows[0].ticker == "SOL"
    assert rows[0].intended == pytest.approx(95.0)
    assert rows[0].fill == pytest.approx(94.0)
    assert rows[0].delta_dollars == pytest.approx(-2.0)
    assert rows[0].delta_r == pytest.approx(-0.20)
    assert rows[0].fee == pytest.approx(1.50)
    zero_risk = Trade(
        ticker="ETH",
        product_id="ETH-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=250.0,
        take_profit=110.0,
        stop_loss=95.0,
        initial_risk_per_unit=0.0,
        time_stop_at=closed_at,
        exit_price=94.0,
        exit_reason=ExitReason.STOP_LOSS,
        realized_pnl=-6.0,
        fees_paid=0.0,
        opened_at=closed_at,
        closed_at=closed_at,
    )
    assert stop_loss_r_slip(zero_risk) == 0.0


def test_weekly_report_stop_loss_rows_and_week_summary(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(
        store,
        "SOL",
        -6.0,
        closed_at=end - timedelta(hours=2),
        stop_loss=95.0,
        exit_price=94.0,
        exit_reason=ExitReason.STOP_LOSS,
        fees=1.50,
        initial_risk_per_unit=5.0,
        original_qty=1.0,
        qty=1.0,
    )
    _closed_trade(
        store,
        "ETH",
        -4.0,
        closed_at=end - timedelta(hours=3),
        stop_loss=95.0,
        exit_price=94.5,
        exit_reason=ExitReason.STOP_LOSS,
        fees=0.50,
        initial_risk_per_unit=5.0,
        original_qty=1.0,
        qty=1.0,
    )
    _closed_trade(
        store,
        "BTC",
        2.0,
        strategy="swing",
        closed_at=end - timedelta(hours=4),
        stop_loss=95.0,
        exit_price=98.0,
        exit_reason=ExitReason.TRAILING_STOP,
        trailing_stop=99.0,
        fees=1.00,
        initial_risk_per_unit=5.0,
    )
    _closed_trade(
        store,
        "HYPE",
        8.0,
        closed_at=end - timedelta(hours=5),
        exit_reason=ExitReason.TAKE_PROFIT,
        fees=1.00,
    )

    _, body = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)
    assert "STOP_LOSS intended vs fill:" in body
    assert "SOL   intended $95.000000  fill $94.000000" in body
    assert "ETH   intended $95.000000  fill $94.500000" in body
    assert "Δ$ $-1.00  ΔR -0.20  fee $1.50" in body
    assert "Δ$ $-0.50  ΔR -0.10  fee $0.50" in body
    summary = format_stop_loss_week_summary(aggregate_stop_loss_fills(list(store.closed_trades())))
    assert summary in body
    assert "STOP_LOSS week: n=2  mean Δ$=$-0.75  mean ΔR=-0.15  fees $2.00" in body
    assert "STOP_LOSS fill Δ by ticker × notional:" in body
    assert "ETH   <$300      n=1  mean Δ$=$-0.50  mean ΔR=-0.10  fees $0.50" in body
    assert "SOL   <$300      n=1  mean Δ$=$-1.00  mean ΔR=-0.20  fees $1.50" in body
    # TRAILING_STOP fill must not be treated as a hard-stop row.
    assert "intended $95.000000  fill $98.000000" not in body
    compare = build_compare_report(store, ["intraday", "swing"])
    assert "STOP_LOSS intended vs fill:" not in compare
    assert "STOP_LOSS fill Δ by ticker × notional:" in compare
    assert "ETH   <$300      n=1  mean Δ$=$-0.50  mean ΔR=-0.10  fees $0.50" in compare
    assert "SOL   <$300      n=1  mean Δ$=$-1.00  mean ΔR=-0.20  fees $1.50" in compare


def test_weekly_report_mfe_event_snapshot_section(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(
        store,
        "SOL",
        8.0,
        closed_at=end - timedelta(hours=2),
        exit_snapshot={
            "mfe_r_at_partial": 1.20,
            "realized_r_at_partial": 0.45,
            "mfe_r_at_trail": 1.50,
            "realized_r_at_trail": 0.80,
        },
    )
    _, body = build_weekly_report(store, ["intraday"], start, end, UTC)
    assert "MFE / realized R at partial & trail:" in body
    assert (
        "SOL   partial MFE=1.20R realized=0.45R giveback=0.75R  |  "
        "trail MFE=1.50R realized=0.80R giveback=0.70R"
    ) in body
    compare = build_compare_report(store, ["intraday"])
    assert "MFE / realized R at partial & trail:" not in compare


def test_weekly_report_omits_mfe_event_section_without_keys(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(store, "SOL", 8.0, closed_at=end - timedelta(hours=2))
    _, body = build_weekly_report(store, ["intraday"], start, end, UTC)
    assert "MFE / realized R at partial & trail:" not in body


def _stop_loss_trade(
    *,
    ticker: str,
    notional: float,
    exit_price: float,
    fees: float,
    qty: float = 1.0,
    stop_loss: float = 95.0,
    risk: float = 5.0,
    exit_reason=ExitReason.STOP_LOSS,
    trailing_stop: float = 0.0,
) -> Trade:
    closed_at = datetime(2026, 8, 16, 20, tzinfo=UTC)
    return Trade(
        ticker=ticker,
        product_id=f"{ticker}-USD",
        qty=qty,
        original_qty=qty,
        entry_price=100.0,
        entry_notional=notional,
        take_profit=110.0,
        stop_loss=stop_loss,
        trailing_stop=trailing_stop,
        initial_risk_per_unit=risk,
        time_stop_at=closed_at,
        exit_price=exit_price,
        exit_reason=exit_reason,
        realized_pnl=(exit_price - 100.0) * qty,
        fees_paid=fees,
        opened_at=closed_at,
        closed_at=closed_at,
    )


def test_stop_loss_fill_rollup_means_by_ticker_and_notional_bucket():
    # Two SOL <$300 stops average; SOL $300-500 and BTC <$300 stay separate.
    sol_a = _stop_loss_trade(ticker="SOL", notional=250.0, exit_price=94.0, fees=1.50)
    sol_b = _stop_loss_trade(ticker="SOL", notional=200.0, exit_price=93.0, fees=1.50)
    sol_mid = _stop_loss_trade(ticker="SOL", notional=400.0, exit_price=94.5, fees=0.50)
    btc = _stop_loss_trade(ticker="BTC", notional=250.0, exit_price=94.0, fees=1.00)
    # Empty $500-700 / >=$700 cells and TRAILING_STOP must not appear.
    trail = _stop_loss_trade(
        ticker="ETH",
        notional=250.0,
        exit_price=98.5,
        fees=1.00,
        exit_reason=ExitReason.TRAILING_STOP,
        trailing_stop=99.0,
    )
    take = _stop_loss_trade(
        ticker="HYPE",
        notional=700.0,
        exit_price=110.0,
        fees=1.00,
        exit_reason=ExitReason.TAKE_PROFIT,
    )
    rows = stop_loss_fill_rollup([sol_a, sol_b, sol_mid, btc, trail, take])
    assert [(row.ticker, row.notional_bucket, row.n) for row in rows] == [
        ("BTC", "<$300", 1),
        ("SOL", "<$300", 2),
        ("SOL", "$300-500", 1),
    ]
    by_key = {(row.ticker, row.notional_bucket): row for row in rows}
    sol_small = by_key[("SOL", "<$300")]
    assert sol_small.mean_delta_dollars == pytest.approx(-1.50)
    assert sol_small.mean_delta_r == pytest.approx(-0.30)
    assert sol_small.fees == pytest.approx(3.00)
    sol_mid_row = by_key[("SOL", "$300-500")]
    assert sol_mid_row.mean_delta_dollars == pytest.approx(-0.50)
    assert sol_mid_row.mean_delta_r == pytest.approx(-0.10)
    assert sol_mid_row.fees == pytest.approx(0.50)
    btc_row = by_key[("BTC", "<$300")]
    assert btc_row.mean_delta_dollars == pytest.approx(-1.00)
    assert btc_row.mean_delta_r == pytest.approx(-0.20)
    assert btc_row.fees == pytest.approx(1.00)
    assert all(row.n > 0 for row in rows)
    assert all(row.ticker != "ETH" for row in rows)
    assert all(row.ticker != "HYPE" for row in rows)
    assert format_stop_loss_fill_rollup_row(sol_small) == (
        "  SOL   <$300      n=2  mean Δ$=$-1.50  mean ΔR=-0.30  fees $3.00"
    )


def test_stop_loss_fill_rollup_ignores_trailing_stop():
    hard = _stop_loss_trade(ticker="SOL", notional=250.0, exit_price=94.0, fees=1.50)
    trail = _stop_loss_trade(
        ticker="SOL",
        notional=250.0,
        exit_price=98.5,
        fees=2.00,
        exit_reason=ExitReason.TRAILING_STOP,
        trailing_stop=99.0,
    )
    rows = stop_loss_fill_rollup([hard, trail])
    assert len(rows) == 1
    assert rows[0].ticker == "SOL"
    assert rows[0].n == 1
    assert rows[0].mean_delta_dollars == pytest.approx(stop_loss_dollar_slip(hard))
    assert rows[0].mean_delta_r == pytest.approx(stop_loss_r_slip(hard))
    assert rows[0].fees == pytest.approx(1.50)
    assert is_hard_stop_loss(trail) is False


def test_stop_loss_fill_rollup_omits_empty_cells_and_empty_section(tmp_path):
    assert stop_loss_fill_rollup([]) == []
    take = _stop_loss_trade(
        ticker="SOL",
        notional=250.0,
        exit_price=110.0,
        fees=1.00,
        exit_reason=ExitReason.TAKE_PROFIT,
    )
    trail = _stop_loss_trade(
        ticker="BTC",
        notional=400.0,
        exit_price=98.5,
        fees=1.00,
        exit_reason=ExitReason.TRAILING_STOP,
        trailing_stop=99.0,
    )
    assert stop_loss_fill_rollup([take, trail]) == []

    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(
        store,
        "SOL",
        8.0,
        closed_at=end - timedelta(hours=2),
        exit_reason=ExitReason.TAKE_PROFIT,
        fees=1.00,
    )
    _closed_trade(
        store,
        "BTC",
        2.0,
        strategy="swing",
        closed_at=end - timedelta(hours=3),
        exit_reason=ExitReason.TRAILING_STOP,
        trailing_stop=99.0,
        exit_price=98.0,
        fees=1.00,
    )
    _, weekly = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)
    compare = build_compare_report(store, ["intraday", "swing"])
    assert "STOP_LOSS fill Δ by ticker × notional:" not in weekly
    assert "STOP_LOSS fill Δ by ticker × notional:" not in compare
    assert "STOP_LOSS intended vs fill:" not in weekly
    assert "STOP_LOSS intended vs fill:" not in compare


def test_intraday_notional_fee_is_dedicated_closed_subsection(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(
        store,
        "SOL",
        4.0,
        strategy="intraday",
        closed_at=end - timedelta(hours=2),
        notional=250.0,
        fees=1.00,
    )
    _closed_trade(
        store,
        "SOL",
        -1.0,
        strategy="intraday",
        closed_at=end - timedelta(hours=3),
        notional=400.0,
        fees=2.00,
    )
    _closed_trade(
        store,
        "BTC",
        6.0,
        strategy="swing",
        closed_at=end - timedelta(hours=4),
        notional=700.0,
        fees=3.50,
    )

    closed = list(store.closed_trades())
    rows = intraday_notional_fee_rows(closed)
    assert [row.ticker for row in rows] == ["SOL", "SOL"]
    by_notional = {row.entry_notional: row for row in rows}
    assert set(by_notional) == {250.0, 400.0}
    assert by_notional[250.0].fees == pytest.approx(1.00)
    assert by_notional[250.0].fee_pct == pytest.approx(1.00 / 250.0)
    assert by_notional[400.0].fees == pytest.approx(2.00)
    assert by_notional[400.0].fee_pct == pytest.approx(2.00 / 400.0)
    assert all(row.ticker != "BTC" for row in rows)

    _, weekly = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)
    compare = build_compare_report(store, ["intraday", "swing"])
    for body in (weekly, compare):
        assert "Intraday notional vs fee:" in body
        assert "SOL   notional $   250.00  fees $    1.00  fee%  0.40%" in body
        assert "SOL   notional $   400.00  fees $    2.00  fee%  0.50%" in body
        assert "TOTAL notional $   650.00  fees $    3.00  fee%  0.46%" in body
        # Swing size must not appear as an intraday row (global buckets still may).
        assert "BTC   notional $   700.00" not in body


def test_intraday_notional_fee_omitted_when_no_closed_intraday(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(
        store,
        "BTC",
        3.0,
        strategy="swing",
        closed_at=end - timedelta(hours=2),
        notional=500.0,
        fees=1.0,
    )
    _, weekly = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)
    compare = build_compare_report(store, ["intraday", "swing"])
    assert "Intraday notional vs fee:" not in weekly
    assert "Intraday notional vs fee:" not in compare
    assert intraday_notional_fee_rows(list(store.closed_trades())) == []


# ---- Trade notifications ----------------------------------------------------


def test_sell_alert_reports_profit_and_loss(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)

    win = _closed_trade(store, "SOL", 15.40, closed_at=end)
    subject, body = trade_closed_alert(win)
    assert "PROFIT" in subject and "+15.40" in subject
    assert "P/L: $+15.40 (+6.16%)" in body
    assert "Exit profile: legacy" in body
    assert "Config fingerprint: legacy" in body
    assert "Exit snapshot: null" in body
    assert "MFE:" in body and "Held:" in body

    loss = _closed_trade(store, "BTC", -8.10, closed_at=end)
    subject, body = trade_closed_alert(loss)
    assert "LOSS" in subject and "-8.10" in subject
    assert "Intended stop:" in body
    assert "Δ$:" in body and "ΔR" in body

    trail = _closed_trade(
        store,
        "ETH",
        -2.0,
        closed_at=end,
        exit_reason=ExitReason.TRAILING_STOP,
        trailing_stop=99.0,
        exit_price=98.5,
    )
    _, trail_body = trade_closed_alert(trail)
    assert "Reason: TRAILING_STOP" in trail_body
    assert "Intended stop:" not in trail_body


def test_buy_alert_carries_the_exit_levels(tmp_path):
    store = make_store(tmp_path)
    trade = _closed_trade(store, "SOL", 0.0, closed_at=datetime(2026, 8, 16, 20, tzinfo=UTC))
    subject, body = trade_opened_alert(trade, 250.0, "atr=0.47%/bar")
    assert subject.startswith("BUY SOL")
    assert "Take-profit" in body and "Stop-loss" in body
    assert "PAPER" in body
    assert "Exit profile: legacy" in body
    assert "Config fingerprint: legacy" in body
    assert "Exit snapshot: null" in body
    assert "MFE:" in body and "Held:" in body


def test_trade_alerts_can_be_switched_off():
    assert TradeAlertsConfig(enabled=False).enabled is False


# ---- Gen-8 setup cohort scoreboard ------------------------------------------

GEN8_FP = GEN8_CONFIG_FINGERPRINT_PREFIX + "a" * (64 - len(GEN8_CONFIG_FINGERPRINT_PREFIX))
GEN7_FP = "e788820ab5d0" + "b" * (64 - 12)
COHORT_HEADER = "Setup cohort since gen-8 (fp c95a0ad410f4):"


def _bare_closed_trade(
    *,
    ticker: str = "SOL",
    pnl: float = 1.0,
    fees: float = 1.0,
    notional: float = 250.0,
    setup: str = "breakout_retest",
    config_fingerprint: str = "",
    closed_at: datetime | None = None,
    trade_id: int | None = None,
) -> Trade:
    closed_at = closed_at or datetime(2026, 8, 16, 20, tzinfo=UTC)
    trade = Trade(
        ticker=ticker,
        product_id=f"{ticker}-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=notional,
        take_profit=110.0,
        stop_loss=95.0,
        time_stop_at=closed_at,
        exit_price=100.0 + pnl,
        exit_reason=ExitReason.TAKE_PROFIT if pnl >= 0 else ExitReason.STOP_LOSS,
        realized_pnl=pnl,
        fees_paid=fees,
        setup=setup,
        config_fingerprint=config_fingerprint,
        opened_at=closed_at - timedelta(hours=3),
        closed_at=closed_at,
    )
    if trade_id is not None:
        trade.id = trade_id
    return trade


def _cohort_blocks(body: str) -> tuple[str, str]:
    assert COHORT_HEADER in body
    after = body.split(COHORT_HEADER, 1)[1]
    week, cumulative = after.split("This week:", 1)[1].split("Cumulative:", 1)
    return week, cumulative


def _cost_row_line(block: str, label: str) -> str:
    for line in block.splitlines():
        parts = line.split()
        if parts and parts[0] == label:
            return line
    raise AssertionError(f"{label!r} cost row missing from:\n{block}")


def _cost_row_n(block: str, label: str) -> int:
    return int(_cost_row_line(block, label).split()[1])


def test_trades_matching_config_fingerprint_uses_prefix_only():
    gen8 = _bare_closed_trade(ticker="SOL", config_fingerprint=GEN8_FP)
    gen8_short = _bare_closed_trade(
        ticker="ETH", config_fingerprint=GEN8_CONFIG_FINGERPRINT_PREFIX
    )
    gen7 = _bare_closed_trade(ticker="BTC", config_fingerprint=GEN7_FP)
    empty = _bare_closed_trade(ticker="HYPE", config_fingerprint="")
    missing = _bare_closed_trade(ticker="ZEC", config_fingerprint="")
    missing.config_fingerprint = None  # type: ignore[assignment]

    matched = trades_matching_config_fingerprint(
        [gen8, gen8_short, gen7, empty, missing], GEN8_CONFIG_FINGERPRINT_PREFIX
    )
    assert matched == [gen8, gen8_short]
    assert GEN8_CONFIG_FINGERPRINT_PREFIX == "c95a0ad410f4"


def test_weekly_setup_cohort_filters_fingerprint_and_splits_week_vs_cumulative(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)

    week_gen8 = _closed_trade(
        store,
        "SOL",
        5.0,
        closed_at=end - timedelta(hours=2),
        fees=1.0,
        notional=250.0,
        setup="breakout_retest",
        config_fingerprint=GEN8_FP,
    )
    prior_gen8 = _closed_trade(
        store,
        "BTC",
        -8.0,
        strategy="swing",
        closed_at=start - timedelta(hours=2),
        fees=2.0,
        notional=400.0,
        setup="breakout_close",
        config_fingerprint=GEN8_FP,
    )
    _closed_trade(
        store,
        "ETH",
        9.0,
        closed_at=end - timedelta(hours=3),
        fees=1.0,
        setup="vwap",
        config_fingerprint=GEN7_FP,
    )
    _closed_trade(
        store,
        "HYPE",
        4.0,
        closed_at=end - timedelta(hours=4),
        fees=1.0,
        setup="breakout_retest",
        config_fingerprint="",
    )
    _link_setup(store, week_gen8, "breakout_retest")
    _link_setup(store, prior_gen8, "breakout_close")

    _, body = build_weekly_report(store, ["intraday", "swing"], start, end, UTC)
    assert COHORT_HEADER in body
    assert "This week:" in body
    assert "Cumulative:" in body
    week_block, cum_block = _cohort_blocks(body)
    for name in SETUP_BUCKETS:
        assert name in week_block
        assert name in cum_block
    assert _cost_row_n(week_block, "breakout_retest") == 1
    assert _cost_row_n(week_block, "breakout_close") == 0
    assert _cost_row_n(week_block, "vwap") == 0
    assert _cost_row_n(week_block, "unknown") == 0
    assert _cost_row_n(week_block, "TOTAL") == 1
    assert _cost_row_n(cum_block, "breakout_retest") == 1
    assert _cost_row_n(cum_block, "breakout_close") == 1
    assert _cost_row_n(cum_block, "TOTAL") == 2
    week_total = _cost_row_line(week_block, "TOTAL")
    assert "gross $     6.00" in week_total
    assert "fees $    1.00" in week_total
    assert "net $     5.00" in week_total
    cum_total = _cost_row_line(cum_block, "TOTAL")
    assert "gross $     0.00" in cum_total
    assert "fees $    3.00" in cum_total
    assert "net $    -3.00" in cum_total


def test_weekly_setup_cohort_prints_empty_week_when_only_cumulative_has_gen8(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(
        store,
        "SOL",
        5.0,
        closed_at=start - timedelta(hours=2),
        fees=1.0,
        setup="breakout_retest",
        config_fingerprint=GEN8_FP,
    )
    _closed_trade(
        store,
        "ETH",
        2.0,
        closed_at=end - timedelta(hours=2),
        setup="vwap",
        config_fingerprint=GEN7_FP,
    )

    _, body = build_weekly_report(store, ["intraday"], start, end, UTC)
    week_block, cum_block = _cohort_blocks(body)
    assert _cost_row_n(week_block, "TOTAL") == 0
    assert _cost_row_n(cum_block, "TOTAL") == 1
    assert _cost_row_n(cum_block, "breakout_retest") == 1
    for name in SETUP_BUCKETS:
        assert name in week_block


def test_weekly_setup_cohort_omitted_when_no_gen8_trades(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)
    _closed_trade(
        store,
        "SOL",
        5.0,
        closed_at=end - timedelta(hours=2),
        setup="breakout_retest",
        config_fingerprint=GEN7_FP,
    )
    _closed_trade(
        store,
        "BTC",
        -2.0,
        closed_at=end - timedelta(hours=3),
        setup="vwap",
        config_fingerprint="",
    )

    _, weekly = build_weekly_report(store, ["intraday"], start, end, UTC)
    compare = build_compare_report(store, ["intraday"])
    assert COHORT_HEADER not in weekly
    assert "This week:" not in weekly
    assert "Cumulative:" not in weekly
    assert COHORT_HEADER not in compare
    assert "This week:" not in compare
    assert "Cumulative:" not in compare


def test_compare_setup_cohort_rolling_week_and_cumulative(tmp_path):
    store = make_store(tmp_path)
    now = datetime.now(UTC)
    recent = _closed_trade(
        store,
        "SOL",
        5.0,
        closed_at=now - timedelta(days=1),
        fees=1.0,
        setup="breakout_retest",
        config_fingerprint=GEN8_FP,
    )
    older = _closed_trade(
        store,
        "BTC",
        -8.0,
        strategy="swing",
        closed_at=now - timedelta(days=10),
        fees=2.0,
        setup="vwap",
        config_fingerprint=GEN8_FP,
    )
    _closed_trade(
        store,
        "ETH",
        3.0,
        closed_at=now - timedelta(days=1),
        setup="breakout_close",
        config_fingerprint=GEN7_FP,
    )
    _link_setup(store, recent, "breakout_retest")
    _link_setup(store, older, "vwap")

    body = build_compare_report(store, ["intraday", "swing"])
    assert COHORT_HEADER in body
    week_block, cum_block = _cohort_blocks(body)
    for name in SETUP_BUCKETS:
        assert name in week_block
        assert name in cum_block
    assert _cost_row_n(week_block, "TOTAL") == 1
    assert _cost_row_n(cum_block, "TOTAL") == 2
    assert _cost_row_n(week_block, "breakout_retest") == 1
    assert _cost_row_n(cum_block, "vwap") == 1
    assert _cost_row_n(week_block, "vwap") == 0


def test_compare_setup_cohort_omitted_when_empty_book(tmp_path):
    store = make_store(tmp_path)
    body = build_compare_report(store, ["intraday"], mode="PAPER")
    assert "No closed trades." in body
    assert COHORT_HEADER not in body


# ---- Telegram message splitting ---------------------------------------------


def test_short_messages_are_not_split():
    assert split_message("hello") == ["hello"]


def test_long_reports_split_on_line_boundaries():
    body = "\n".join(f"line {i}" for i in range(1000))
    chunks = split_message(body, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    # No line may be broken across chunks.
    assert "\n".join(chunks).split("\n") == body.split("\n")


def test_a_single_oversized_line_is_hard_split():
    chunks = split_message("x" * 500, limit=200)
    assert all(len(c) <= 200 for c in chunks)
    assert "".join(chunks) == "x" * 500
