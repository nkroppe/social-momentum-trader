"""Human-readable performance summaries for alert channels.

Formatted as plain text rather than Markdown: Telegram's parsers reject
unescaped `_`, `*`, and `.` characters, and a report that fails to send is worse
than one without bold text.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, tzinfo

from ..models import Trade
from ..store import Store
from ..trader.exit_policy import mfe_r


def _aware(dt: datetime) -> datetime:
    """Treat naive timestamps as UTC; SQLite round-trips drop the zone."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _hold_hours(trade: Trade) -> float:
    if trade.closed_at is None:
        return 0.0
    return max((_aware(trade.closed_at) - _aware(trade.opened_at)).total_seconds() / 3600.0, 0.0)


def _pnl_pct(trade: Trade) -> float:
    return (trade.realized_pnl / trade.entry_notional) if trade.entry_notional else 0.0


def _mfe_r(trade: Trade) -> float:
    return mfe_r(
        trade.highest_price or trade.entry_price,
        trade.entry_price,
        trade.initial_risk_per_unit,
    )


def _snapshot_text(trade: Trade) -> str:
    return json.dumps(trade.exit_snapshot, sort_keys=True, separators=(",", ":"))


def _current_hold_hours(trade: Trade) -> float:
    return max(
        (_aware(datetime.now(UTC)) - _aware(trade.opened_at)).total_seconds() / 3600.0,
        0.0,
    )


def format_trade_row(trade: Trade) -> str:
    reason = trade.exit_reason.value if trade.exit_reason else "?"
    return (
        f"{trade.ticker:<5} {trade.strategy:<9} {reason:<15} "
        f"${trade.realized_pnl:>8,.2f}  {_pnl_pct(trade):>+7.2%}  "
        f"{_hold_hours(trade):>5.1f}h MFE={_mfe_r(trade):.2f}R "
        f"profile={trade.exit_profile_label or 'legacy'} "
        f"fp={(trade.config_fingerprint or '-')[:12]}"
    )


def trade_opened_alert(trade: Trade, notional: float, exit_note: str) -> tuple[str, str]:
    """Subject and body for an entry notification."""
    subject = f"BUY {trade.ticker} [{trade.strategy}]"
    body = "\n".join(
        [
            f"Bought {trade.qty:.8f} {trade.ticker} @ ${trade.entry_price:,.6f}",
            f"Size: ${notional:,.2f}",
            f"Take-profit: ${trade.take_profit:,.6f}",
            f"Stop-loss: ${trade.stop_loss:,.6f}",
            f"Time-stop: {_aware(trade.time_stop_at):%Y-%m-%d %H:%M} UTC",
            f"Mode: {'LIVE' if trade.is_live else 'PAPER'}",
            f"Exit profile: {trade.exit_profile_label or 'legacy'}",
            f"Config fingerprint: {trade.config_fingerprint or 'legacy'}",
            f"Exit snapshot: {_snapshot_text(trade)}",
            f"MFE: {_mfe_r(trade):.2f}R",
            "Held: 0.0h",
            f"Levels: {exit_note}",
        ]
    )
    return subject, body


def trade_closed_alert(trade: Trade) -> tuple[str, str]:
    """Subject and body for an exit notification, carrying realized P/L."""
    pnl = trade.realized_pnl
    result = "PROFIT" if pnl >= 0 else "LOSS"
    reason = trade.exit_reason.value if trade.exit_reason else "?"

    subject = f"SELL {trade.ticker} [{trade.strategy}] {result} ${pnl:+,.2f}"
    body = "\n".join(
        [
            f"Sold {trade.qty:.8f} {trade.ticker} @ ${trade.exit_price:,.6f}",
            f"Entry: ${trade.entry_price:,.6f}",
            f"Reason: {reason}",
            "",
            f"P/L: ${pnl:+,.2f} ({_pnl_pct(trade):+.2%})",
            f"Fees: ${trade.fees_paid:,.2f}",
            f"Held: {_hold_hours(trade):.1f}h",
            f"MFE: {_mfe_r(trade):.2f}R",
            f"Exit profile: {trade.exit_profile_label or 'legacy'}",
            f"Config fingerprint: {trade.config_fingerprint or 'legacy'}",
            f"Exit snapshot: {_snapshot_text(trade)}",
            f"Mode: {'LIVE' if trade.is_live else 'PAPER'}",
        ]
    )
    return subject, body


def trade_partial_alert(
    trade: Trade,
    sold_qty: float,
    exit_price: float,
    partial_pnl: float,
) -> tuple[str, str]:
    """Notification for the first scale-out sell."""
    result = "PROFIT" if partial_pnl >= 0 else "LOSS"
    subject = f"PARTIAL SELL {trade.ticker} [{trade.strategy}] {result} ${partial_pnl:+,.2f}"
    body = "\n".join(
        [
            f"Sold {sold_qty:.8f} {trade.ticker} @ ${exit_price:,.6f}",
            f"Remaining: {trade.qty:.8f} {trade.ticker}",
            f"Entry: ${trade.entry_price:,.6f}",
            f"Partial P/L: ${partial_pnl:+,.2f}",
            f"Trailing stop: ${trade.trailing_stop:,.6f}",
            f"Held: {_current_hold_hours(trade):.1f}h",
            f"MFE: {_mfe_r(trade):.2f}R",
            f"Exit profile: {trade.exit_profile_label or 'legacy'}",
            f"Config fingerprint: {trade.config_fingerprint or 'legacy'}",
            f"Exit snapshot: {_snapshot_text(trade)}",
            f"Mode: {'LIVE' if trade.is_live else 'PAPER'}",
        ]
    )
    return subject, body


def build_weekly_report(
    store: Store,
    strategies: Sequence[str],
    start: datetime,
    end: datetime,
    display_tz: tzinfo,
    *,
    mode: str = "PAPER",
    max_trades_listed: int = 40,
    mark_price: Callable[[str], float] | None = None,
) -> tuple[str, str]:
    """Subject and body summarizing every trade closed in [start, end)."""
    local_start = start.astimezone(display_tz)
    local_end = end.astimezone(display_tz)

    closed = list(store.closed_trades_between(start, end))
    opened = store.count_trades_opened_between(start, end)
    net = sum(t.realized_pnl for t in closed)
    wins = sum(1 for t in closed if t.realized_pnl > 0)
    losses = sum(1 for t in closed if t.realized_pnl < 0)
    breakeven = len(closed) - wins - losses
    fees = sum(t.fees_paid for t in closed)

    # A window ending in the future is the week currently in progress, which the
    # CLI renders on demand; say so rather than implying it is final.
    partial = " (in progress)" if end > datetime.now(UTC) else ""
    subject = f"Weekly report {local_start:%b %d} - {local_end:%b %d}{partial}: ${net:+,.2f}"

    lines = [
        f"Week of {local_start:%a %b %d, %Y %I:%M %p} to {local_end:%a %b %d, %Y %I:%M %p} "
        f"({local_end:%Z}){partial}",
        f"Mode: {mode}",
        "",
        f"Trades opened:  {opened}",
        f"Trades closed:  {len(closed)}",
        f"Win rate:       {(wins / len(closed)) if closed else 0:.0%} "
        f"({wins}W / {losses}L"
        f"{f' / {breakeven}BE' if breakeven else ''})",
        f"Fees paid:      ${fees:,.2f}",
        f"NET P/L:        ${net:+,.2f}",
    ]

    if closed:
        lines += ["", "Completed trades:"]
        shown = closed[:max_trades_listed]
        lines += [f"  {format_trade_row(t)}" for t in shown]
        if len(closed) > len(shown):
            lines.append(f"  ... and {len(closed) - len(shown)} more")
    else:
        lines += ["", "No trades closed this week."]

    if len(strategies) > 1:
        lines += ["", "By strategy:"]
        for name in strategies:
            rows = [t for t in closed if t.strategy == name]
            s_pnl = sum(t.realized_pnl for t in rows)
            s_wins = sum(1 for t in rows if t.realized_pnl > 0)
            rate = (s_wins / len(rows)) if rows else 0.0
            lines.append(f"  {name:<9} {len(rows):>2} closed  {rate:>4.0%} win  ${s_pnl:>9,.2f}")

    open_trades = store.open_trades()
    if open_trades:
        lines += ["", f"Still open: {len(open_trades)}"]
        for t in open_trades:
            if mark_price is None:
                lines.append(
                    f"  {t.ticker:<5} {t.strategy:<9} entry ${t.entry_price:,.6f} "
                    f"held={_current_hold_hours(t):.1f}h "
                    f"MFE={_mfe_r(t):.2f}R profile={t.exit_profile_label or 'legacy'} "
                    f"fp={(t.config_fingerprint or '-')[:12]} snapshot={_snapshot_text(t)}"
                )
                continue
            try:
                unrealized = (mark_price(t.product_id) - t.entry_price) * t.qty
            except Exception:  # noqa: BLE001 - a quote failure must not lose the report
                lines.append(
                    f"  {t.ticker:<5} {t.strategy:<9} entry ${t.entry_price:,.6f} "
                    f"MFE={_mfe_r(t):.2f}R profile={t.exit_profile_label or 'legacy'}"
                )
                continue
            lines.append(
                f"  {t.ticker:<5} {t.strategy:<9} entry ${t.entry_price:,.6f}  "
                f"unrealized ${unrealized:+,.2f} "
                f"held={_current_hold_hours(t):.1f}h "
                f"MFE={_mfe_r(t):.2f}R profile={t.exit_profile_label or 'legacy'} "
                f"fp={(t.config_fingerprint or '-')[:12]} snapshot={_snapshot_text(t)}"
            )

    lines += ["", f"Lifetime realized P/L: ${store.total_realized_pnl():+,.2f}"]
    return subject, "\n".join(lines)
