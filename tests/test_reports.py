"""Weekly scheduling, report formatting, and trade notifications."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from _helpers import make_store

from smt.config import TradeAlertsConfig, WeeklyReportConfig
from smt.models import ExitReason, Trade, TradeStatus
from smt.ops.alerts import split_message
from smt.ops.reports import (
    SETUP_BUCKETS,
    UNKNOWN_SETUP,
    aggregate_cost_stats,
    build_compare_report,
    build_weekly_report,
    classify_setup,
    resolve_setup_name,
    setup_cost_stats,
    trade_closed_alert,
    trade_fee_pct_of_notional,
    trade_gross_pnl,
    trade_opened_alert,
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
):
    trade = Trade(
        ticker=ticker,
        strategy=strategy,
        product_id=f"{ticker}-USD",
        is_live=False,
        status=TradeStatus.CLOSED,
        qty=1.0,
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
    subject, _ = trade_closed_alert(loss)
    assert "LOSS" in subject and "-8.10" in subject


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
