"""Weekly scheduling, report formatting, and trade notifications."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from _helpers import make_store

from smt.config import TradeAlertsConfig, WeeklyReportConfig
from smt.models import ExitReason, Trade, TradeStatus
from smt.ops.alerts import split_message
from smt.ops.reports import build_weekly_report, trade_closed_alert, trade_opened_alert
from smt.ops.schedule import WeeklyScheduler

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


def _closed_trade(store, ticker, pnl, *, strategy="intraday", closed_at, notional=250.0):
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
        fees_paid=1.0,
        opened_at=closed_at - timedelta(hours=3),
        closed_at=closed_at,
    )
    return store.add_trade(trade)


def test_weekly_report_totals_only_the_window(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)
    start = end - timedelta(days=7)

    _closed_trade(store, "SOL", 15.40, closed_at=end - timedelta(days=1))
    _closed_trade(store, "BTC", -8.10, strategy="swing", closed_at=end - timedelta(days=2))
    # Outside the window on both sides.
    _closed_trade(store, "ETH", 999.0, closed_at=start - timedelta(hours=1))
    _closed_trade(store, "LINK", 999.0, closed_at=end + timedelta(hours=1))

    subject, body = build_weekly_report(
        store, ["intraday", "swing"], start, end, UTC, mode="PAPER"
    )

    assert "+7.30" in subject
    assert "Trades closed:  2" in body
    assert "NET P/L:        $+7.30" in body
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


# ---- Trade notifications ----------------------------------------------------


def test_sell_alert_reports_profit_and_loss(tmp_path):
    store = make_store(tmp_path)
    end = datetime(2026, 8, 16, 20, tzinfo=UTC)

    win = _closed_trade(store, "SOL", 15.40, closed_at=end)
    subject, body = trade_closed_alert(win)
    assert "PROFIT" in subject and "+15.40" in subject
    assert "P/L: $+15.40 (+6.16%)" in body

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
