"""Human-readable performance summaries for alert channels.

Formatted as plain text rather than Markdown: Telegram's parsers reject
unescaped `_`, `*`, and `.` characters, and a report that fails to send is worse
than one without bold text.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, tzinfo

from ..models import ExitReason, Trade
from ..store import Store
from ..trader.exit_policy import mfe_r

UNKNOWN_SETUP = "unknown"
SETUP_BUCKETS = ("breakout_retest", "breakout_close", "vwap", "unknown")
NOTIONAL_BUCKETS = ("<$300", "$300-500", "$500-700", ">=$700")
MFE_R_EPSILON = 1e-12
# Tiny favorable excursions make realized_R / MFE_R explode; the capture mean
# keeps only trades whose MFE_R meets this floor. trade_mfe_capture still uses
# MFE_R_EPSILON so other callers can see the raw ratio.
MFE_CAPTURE_MIN_MFE_R = 0.25
GEN8_CONFIG_FINGERPRINT_PREFIX = "c95a0ad410f4"
COHORT_STRATEGY_NAMES = ("intraday", "swing", "bear_rally")
SMALL_N_THRESHOLD = 5
_PREFERRED_SETUPS = SETUP_BUCKETS[:-1]
MFE_EVENT_SNAPSHOT_KEYS = (
    "mfe_r_at_partial",
    "realized_r_at_partial",
    "mfe_r_at_trail",
    "realized_r_at_trail",
)
SHADOW_GATE_SNAPSHOT_KEYS = (
    "shadow_gate_fee_hurdle_r_max",
    "shadow_block_fee_hurdle",
    "shadow_block_cross_sleeve",
    "shadow_cross_sleeve_holders",
    "shadow_gates_version",
)
# Hours [start, end) UTC, tagged by trade.opened_at.
SESSION_BOUNDARIES_UTC = (("Asia", 0, 8), ("Europe", 8, 13), ("US", 13, 21), ("Late-US", 21, 24))
ENTRY_SIGNAL_PRICE_KEY = "entry_signal_price"
ENTRY_QUOTE_PRICE_KEY = "entry_quote_price"
ENTRY_FILL_PRICE_KEY = "entry_fill_price"
ENTRY_SLIPPAGE_BPS_VS_SIGNAL_KEY = "entry_slippage_bps_vs_signal"
ENTRY_SLIPPAGE_USD_VS_SIGNAL_KEY = "entry_slippage_usd_vs_signal"
ENTRY_SLIPPAGE_BPS_VS_QUOTE_KEY = "entry_slippage_bps_vs_quote"
ENTRY_SLIPPAGE_USD_VS_QUOTE_KEY = "entry_slippage_usd_vs_quote"


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


def trade_gross_pnl(trade: Trade) -> float:
    """Gross P/L is net realized plus round-trip fees; slippage stays in the fill."""
    return float(trade.realized_pnl) + float(trade.fees_paid)


def trade_fee_legs_notional(trade: Trade) -> float:
    """Entry + optional paper-partial + remaining-qty exit notionals for a fee what-if.

    Entry uses ``entry_price * original_qty``, falling back to ``qty`` when
    ``original_qty`` is missing/zero, then to ``entry_notional`` when price is
    missing. The partial leg is ``take_profit * (original_qty - qty)`` only when
    ``partial_taken`` and ``original_qty > qty``. Final exit is ``exit_price * qty``.
    """
    qty = float(trade.qty or 0.0)
    original_qty = float(getattr(trade, "original_qty", 0.0) or 0.0)
    entry_qty = original_qty if original_qty else qty
    entry_price = float(trade.entry_price or 0.0)
    entry_leg = entry_price * entry_qty if entry_price else float(trade.entry_notional or 0.0)
    partial_leg = 0.0
    if getattr(trade, "partial_taken", False) and original_qty > qty:
        partial_leg = float(trade.take_profit or 0.0) * (original_qty - qty)
    exit_leg = float(trade.exit_price or 0.0) * qty
    return entry_leg + partial_leg + exit_leg


def trade_net_at_fee_rate(trade: Trade, rate: float) -> float:
    """Gross P/L minus fee-leg notional charged at an alternate per-side rate."""
    return trade_gross_pnl(trade) - trade_fee_legs_notional(trade) * rate


def net_at_fee_rate(trades: Sequence[Trade], rate: float) -> float:
    return sum(trade_net_at_fee_rate(trade, rate) for trade in trades)


def trade_fee_pct_of_notional(trade: Trade) -> float:
    notional = float(trade.entry_notional or 0.0)
    return (float(trade.fees_paid) / notional) if notional else 0.0


@dataclass(frozen=True)
class CostStats:
    """Closed-trade cost rollup. Headline win rate is net (realized_pnl > 0)."""

    n: int = 0
    net_wins: int = 0
    net_losses: int = 0
    net_breakeven: int = 0
    gross_wins: int = 0
    gross_losses: int = 0
    gross_breakeven: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    entry_notional: float = 0.0
    hold_seconds: float = 0.0

    @property
    def net_win_rate(self) -> float:
        return self.net_wins / self.n if self.n else 0.0

    @property
    def gross_win_rate(self) -> float:
        return self.gross_wins / self.n if self.n else 0.0

    @property
    def fee_pct_of_notional(self) -> float:
        return (self.fees / self.entry_notional) if self.entry_notional else 0.0

    @property
    def fee_to_gross(self) -> float:
        return (self.fees / self.gross_pnl) if self.gross_pnl else 0.0

    @property
    def avg_hold_hours(self) -> float:
        return (self.hold_seconds / self.n / 3600.0) if self.n else 0.0


@dataclass(frozen=True)
class MfeCaptureStats:
    """Mean/median realized_R / MFE_R over trades with MFE_R >= min floor.

    ``n`` is the used count. ``n_excluded`` is trades with MFE_R above
    ``MFE_R_EPSILON`` but below the capture floor. Not capped at 1.0.
    """

    n: int = 0
    n_excluded: int = 0
    mean: float = 0.0
    median: float = 0.0


@dataclass(frozen=True)
class StopLossFill:
    """Hard-stop intended vs fill. TRAILING_STOP is excluded; intended is stop_loss."""

    ticker: str
    intended: float
    fill: float
    delta_dollars: float
    delta_r: float
    fee: float


@dataclass(frozen=True)
class StopLossFillStats:
    n: int = 0
    mean_delta_dollars: float = 0.0
    mean_delta_r: float = 0.0
    fees: float = 0.0


@dataclass(frozen=True)
class StopLossFillRollupRow:
    """Closed STOP_LOSS fill-delta means for one ticker × notional cell."""

    ticker: str
    notional_bucket: str
    n: int
    mean_delta_dollars: float
    mean_delta_r: float
    fees: float


@dataclass(frozen=True)
class IntradayNotionalFee:
    """Closed intraday row: size vs round-trip fee. Not a filtered global bucket."""

    ticker: str
    entry_notional: float
    fees: float
    fee_pct: float


@dataclass(frozen=True)
class FeeHurdleRow:
    """One closed trade with a persisted fee_hurdle_r snapshot key."""

    ticker: str
    strategy: str
    opened_at: datetime
    hurdle_r: float
    mfe_r: float
    net_pnl: float
    non_starter: bool


@dataclass(frozen=True)
class FeeHurdleStrategyStats:
    """Fee-hurdle skip / non-starter counts for one strategy."""

    strategy: str
    with_hurdle: int = 0
    without_hurdle: int = 0
    non_starters: int = 0


@dataclass(frozen=True)
class FeeHurdleSummary:
    """Fee-hurdle rows plus skip / non-starter counts, overall and per strategy."""

    rows: tuple[FeeHurdleRow, ...] = ()
    with_hurdle: int = 0
    without_hurdle: int = 0
    non_starters: int = 0
    by_strategy: tuple[FeeHurdleStrategyStats, ...] = ()


@dataclass(frozen=True)
class OverlapMember:
    """One trade in a same-ticker cross-sleeve overlap group."""

    trade_id: int | None
    strategy: str
    ticker: str
    opened_at: datetime
    closed_at: datetime | None
    net_pnl: float | None


@dataclass(frozen=True)
class CrossSleeveOverlapGroup:
    """Overlapping open intervals on one ticker from two or more strategies."""

    ticker: str
    members: tuple[OverlapMember, ...]
    overlap_start: datetime
    overlap_end: datetime
    combined_net_pnl: float
    open_count: int


@dataclass(frozen=True)
class CrossSleeveOverlapSummary:
    groups: tuple[CrossSleeveOverlapGroup, ...] = ()
    combined_net_pnl: float = 0.0


@dataclass(frozen=True)
class EntrySlippageStats:
    """Fill vs signal / quote for closed trades that carry forward-only keys."""

    n: int = 0
    n_signal: int = 0
    n_quote: int = 0
    mean_bps_vs_signal: float = 0.0
    median_bps_vs_signal: float = 0.0
    mean_bps_vs_quote: float = 0.0
    median_bps_vs_quote: float = 0.0
    total_usd_vs_signal: float = 0.0


@dataclass(frozen=True)
class ShadowGateCohortStats:
    """Net dollars and mean net R for one flagged or not-flagged cohort."""

    n: int = 0
    net_pnl: float = 0.0
    mean_net_r: float = 0.0


@dataclass(frozen=True)
class ShadowGateSplit:
    """Flagged vs not-flagged stats for one shadow gate."""

    flagged: ShadowGateCohortStats = field(default_factory=ShadowGateCohortStats)
    not_flagged: ShadowGateCohortStats = field(default_factory=ShadowGateCohortStats)


@dataclass(frozen=True)
class ShadowGateSummary:
    """Coverage plus per-gate splits for a closed-trade set."""

    coverage_n: int = 0
    no_shadow_data: int = 0
    fee_hurdle: ShadowGateSplit = field(default_factory=ShadowGateSplit)
    cross_sleeve: ShadowGateSplit = field(default_factory=ShadowGateSplit)
    either: ShadowGateSplit = field(default_factory=ShadowGateSplit)


def aggregate_cost_stats(trades: Sequence[Trade]) -> CostStats:
    n = net_w = net_l = net_be = 0
    gross_w = gross_l = gross_be = 0
    gross = fees = net = notional = hold = 0.0
    for trade in trades:
        n += 1
        net_pnl = float(trade.realized_pnl)
        fee = float(trade.fees_paid)
        gross_pnl = net_pnl + fee
        net += net_pnl
        fees += fee
        gross += gross_pnl
        notional += float(trade.entry_notional or 0.0)
        if net_pnl > 0:
            net_w += 1
        elif net_pnl < 0:
            net_l += 1
        else:
            net_be += 1
        if gross_pnl > 0:
            gross_w += 1
        elif gross_pnl < 0:
            gross_l += 1
        else:
            gross_be += 1
        if trade.closed_at is not None:
            hold += max(
                (_aware(trade.closed_at) - _aware(trade.opened_at)).total_seconds(),
                0.0,
            )
    return CostStats(
        n=n,
        net_wins=net_w,
        net_losses=net_l,
        net_breakeven=net_be,
        gross_wins=gross_w,
        gross_losses=gross_l,
        gross_breakeven=gross_be,
        gross_pnl=gross,
        fees=fees,
        net_pnl=net,
        entry_notional=notional,
        hold_seconds=hold,
    )


def classify_setup(*names: str | None) -> str:
    """Map Trade.setup / opportunity setup_name onto the four report buckets.

    Empty strings are skipped so a later source can fill. ``vwap_pullback``
    reports as ``vwap``. Any other name is unknown.
    """
    for raw in names:
        name = str(raw or "").strip()
        if not name:
            continue
        if name == "breakout_retest":
            return "breakout_retest"
        if name == "breakout_close":
            return "breakout_close"
        if name in {"vwap", "vwap_pullback"}:
            return "vwap"
        return UNKNOWN_SETUP
    return UNKNOWN_SETUP


def resolve_setup_name(trade: Trade, linked_setups: dict[int, str]) -> str:
    """Named opportunity setup_name first, then Trade.setup; empty → unknown."""
    trade_id = getattr(trade, "id", None)
    linked = ""
    if trade_id is not None and int(trade_id) in linked_setups:
        linked = linked_setups[int(trade_id)]
    return classify_setup(linked, getattr(trade, "setup", None))


def sort_setup_names(names: Iterable[str]) -> list[str]:
    present = set(names)
    return [name for name in SETUP_BUCKETS if name in present]


def setup_cost_stats(
    trades: Sequence[Trade],
    linked_setups: dict[int, str],
) -> list[tuple[str, CostStats]]:
    """Always emit the four setup buckets so counts sum to closed trades."""
    buckets: dict[str, list[Trade]] = {name: [] for name in SETUP_BUCKETS}
    for trade in trades:
        buckets[resolve_setup_name(trade, linked_setups)].append(trade)
    return [(name, aggregate_cost_stats(buckets[name])) for name in SETUP_BUCKETS]


def trades_matching_config_fingerprint(
    trades: Sequence[Trade], fingerprint_prefix: str
) -> list[Trade]:
    """Closed-trade filter: config_fingerprint starts with prefix."""
    return [
        trade for trade in trades if (trade.config_fingerprint or "").startswith(fingerprint_prefix)
    ]


def fee_rate_what_if_line(
    trades: Sequence[Trade],
    rate: float,
    fingerprint_prefix: str = GEN8_CONFIG_FINGERPRINT_PREFIX,
) -> list[str]:
    """Report-only gen-8 net at an alternate per-side fee rate. Does not mutate trades."""
    cohort = trades_matching_config_fingerprint(trades, fingerprint_prefix)
    stats = aggregate_cost_stats(cohort)
    alt_fees = sum(trade_fee_legs_notional(trade) * rate for trade in cohort)
    recomputed = net_at_fee_rate(cohort, rate)
    return [
        f"What-if fee rate {rate:.2%}/side since gen-8 (fp {fingerprint_prefix}, n={stats.n}): "
        f"net ${recomputed:+,.2f} (gross ${stats.gross_pnl:+,.2f} - fees ${alt_fees:,.2f} "
        f"at alt rate) vs actual net ${stats.net_pnl:+,.2f}. Stored data unchanged."
    ]


def _fee_rate_what_if_section(
    trades: Sequence[Trade],
    fee_rate: float | None,
    fingerprint_prefix: str = GEN8_CONFIG_FINGERPRINT_PREFIX,
) -> list[str]:
    if fee_rate is None:
        return []
    return ["", *fee_rate_what_if_line(trades, fee_rate, fingerprint_prefix)]


def classify_notional(notional: float) -> str:
    """Map entry notional onto the four digest fee buckets."""
    size = float(notional or 0.0)
    if size < 300.0:
        return "<$300"
    if size < 500.0:
        return "$300-500"
    if size < 700.0:
        return "$500-700"
    return ">=$700"


def notional_cost_stats(trades: Sequence[Trade]) -> list[tuple[str, CostStats]]:
    """Always emit the four notional buckets so counts sum to closed trades."""
    buckets: dict[str, list[Trade]] = {name: [] for name in NOTIONAL_BUCKETS}
    for trade in trades:
        buckets[classify_notional(trade.entry_notional)].append(trade)
    return [(name, aggregate_cost_stats(buckets[name])) for name in NOTIONAL_BUCKETS]


def ticker_cost_stats(trades: Sequence[Trade]) -> list[tuple[str, CostStats]]:
    """Closed-trade cost rollup keyed by ticker, sorted for stable digest output."""
    buckets: dict[str, list[Trade]] = {}
    for trade in trades:
        buckets.setdefault(trade.ticker, []).append(trade)
    return [(ticker, aggregate_cost_stats(buckets[ticker])) for ticker in sorted(buckets)]


def strategy_cost_stats(
    trades: Sequence[Trade],
    names: Sequence[str] = COHORT_STRATEGY_NAMES,
) -> list[tuple[str, CostStats]]:
    """Canonical strategy names first (even n=0), then any others sorted."""
    preferred = list(names)
    buckets: dict[str, list[Trade]] = {name: [] for name in preferred}
    extras: dict[str, list[Trade]] = {}
    for trade in trades:
        key = str(trade.strategy or "")
        if key in buckets:
            buckets[key].append(trade)
        else:
            extras.setdefault(key, []).append(trade)
    rows = [(name, aggregate_cost_stats(buckets[name])) for name in preferred]
    rows.extend((name, aggregate_cost_stats(extras[name])) for name in sorted(extras))
    return rows


def trade_risk_dollars(trade: Trade) -> float:
    qty = float(getattr(trade, "original_qty", 0.0) or trade.qty or 0.0)
    return max(float(trade.initial_risk_per_unit or 0.0) * qty, 0.0)


def trade_realized_r(trade: Trade) -> float:
    risk = trade_risk_dollars(trade)
    return (float(trade.realized_pnl) / risk) if risk else 0.0


def is_hard_stop_loss(trade: Trade) -> bool:
    """True only for EXIT_REASON STOP_LOSS. TRAILING_STOP is a different exit."""
    reason = trade.exit_reason
    value = reason.value if isinstance(reason, ExitReason) else reason
    return value == ExitReason.STOP_LOSS


def stop_loss_fill_qty(trade: Trade) -> float:
    return float(getattr(trade, "original_qty", 0.0) or trade.qty or 0.0)


def stop_loss_dollar_slip(trade: Trade) -> float:
    """(fill - intended stop) * qty. Long: negative means a worse fill than stop_loss."""
    qty = stop_loss_fill_qty(trade)
    return (float(trade.exit_price or 0.0) - float(trade.stop_loss or 0.0)) * qty


def stop_loss_r_slip(trade: Trade) -> float:
    """Δ$ / initial risk dollars. Zero when risk fields are missing."""
    risk = trade_risk_dollars(trade)
    return (stop_loss_dollar_slip(trade) / risk) if risk else 0.0


def stop_loss_fill_row(trade: Trade) -> StopLossFill:
    return StopLossFill(
        ticker=trade.ticker,
        intended=float(trade.stop_loss or 0.0),
        fill=float(trade.exit_price or 0.0),
        delta_dollars=stop_loss_dollar_slip(trade),
        delta_r=stop_loss_r_slip(trade),
        fee=float(trade.fees_paid or 0.0),
    )


def stop_loss_fills(trades: Sequence[Trade]) -> list[StopLossFill]:
    return [stop_loss_fill_row(trade) for trade in trades if is_hard_stop_loss(trade)]


def aggregate_stop_loss_fills(trades: Sequence[Trade]) -> StopLossFillStats:
    rows = stop_loss_fills(trades)
    n = len(rows)
    if not n:
        return StopLossFillStats()
    return StopLossFillStats(
        n=n,
        mean_delta_dollars=sum(row.delta_dollars for row in rows) / n,
        mean_delta_r=sum(row.delta_r for row in rows) / n,
        fees=sum(row.fee for row in rows),
    )


def format_stop_loss_fill_row(row: StopLossFill) -> str:
    return (
        f"  {row.ticker:<5} intended ${row.intended:,.6f}  fill ${row.fill:,.6f}  "
        f"Δ$ ${row.delta_dollars:>+,.2f}  ΔR {row.delta_r:>+.2f}  fee ${row.fee:,.2f}"
    )


def format_stop_loss_week_summary(stats: StopLossFillStats) -> str:
    return (
        f"STOP_LOSS week: n={stats.n}  mean Δ$=${stats.mean_delta_dollars:+,.2f}  "
        f"mean ΔR={stats.mean_delta_r:+.2f}  fees ${stats.fees:,.2f}"
    )


def _stop_loss_fill_section(trades: Sequence[Trade]) -> list[str]:
    rows = stop_loss_fills(trades)
    if not rows:
        return []
    lines = ["", "STOP_LOSS intended vs fill:"]
    lines.extend(format_stop_loss_fill_row(row) for row in rows)
    lines.append(format_stop_loss_week_summary(aggregate_stop_loss_fills(trades)))
    return lines


def stop_loss_fill_rollup(trades: Sequence[Trade]) -> list[StopLossFillRollupRow]:
    """Mean STOP_LOSS fill Δ by ticker × notional. Empty cells are omitted."""
    groups: dict[tuple[str, str], list[Trade]] = {}
    for trade in trades:
        if not is_hard_stop_loss(trade):
            continue
        key = (trade.ticker, classify_notional(trade.entry_notional))
        groups.setdefault(key, []).append(trade)
    bucket_rank = {name: i for i, name in enumerate(NOTIONAL_BUCKETS)}
    rows: list[StopLossFillRollupRow] = []
    for (ticker, bucket), group in groups.items():
        stats = aggregate_stop_loss_fills(group)
        rows.append(
            StopLossFillRollupRow(
                ticker=ticker,
                notional_bucket=bucket,
                n=stats.n,
                mean_delta_dollars=stats.mean_delta_dollars,
                mean_delta_r=stats.mean_delta_r,
                fees=stats.fees,
            )
        )
    rows.sort(key=lambda row: (row.ticker, bucket_rank[row.notional_bucket]))
    return rows


def format_stop_loss_fill_rollup_row(row: StopLossFillRollupRow) -> str:
    return (
        f"  {row.ticker:<5} {row.notional_bucket:<10} n={row.n}  "
        f"mean Δ$=${row.mean_delta_dollars:+,.2f}  "
        f"mean ΔR={row.mean_delta_r:+.2f}  fees ${row.fees:,.2f}"
    )


def _stop_loss_fill_rollup_section(trades: Sequence[Trade]) -> list[str]:
    rows = stop_loss_fill_rollup(trades)
    if not rows:
        return []
    lines = ["", "STOP_LOSS fill Δ by ticker × notional:"]
    lines.extend(format_stop_loss_fill_rollup_row(row) for row in rows)
    return lines


def snapshot_event_float(trade: Trade, key: str) -> float | None:
    """Read one exit_snapshot event float. Missing or non-numeric keys are omitted."""
    snapshot = trade.exit_snapshot
    if not isinstance(snapshot, dict) or key not in snapshot:
        return None
    try:
        return float(snapshot[key])
    except (TypeError, ValueError):
        return None


def has_mfe_event_snapshot(trade: Trade) -> bool:
    snapshot = trade.exit_snapshot
    if not isinstance(snapshot, dict):
        return False
    return any(key in snapshot for key in MFE_EVENT_SNAPSHOT_KEYS)


def _event_r_clause(prefix: str, mfe: float | None, realized: float | None) -> str | None:
    if mfe is None and realized is None:
        return None
    parts = [prefix]
    if mfe is not None:
        parts.append(f"MFE={mfe:.2f}R")
    if realized is not None:
        parts.append(f"realized={realized:.2f}R")
    if mfe is not None and realized is not None:
        parts.append(f"giveback={mfe - realized:.2f}R")
    return " ".join(parts)


def format_mfe_event_row(trade: Trade) -> str:
    clauses: list[str] = []
    partial = _event_r_clause(
        "partial",
        snapshot_event_float(trade, "mfe_r_at_partial"),
        snapshot_event_float(trade, "realized_r_at_partial"),
    )
    trail = _event_r_clause(
        "trail",
        snapshot_event_float(trade, "mfe_r_at_trail"),
        snapshot_event_float(trade, "realized_r_at_trail"),
    )
    if partial:
        clauses.append(partial)
    if trail:
        clauses.append(trail)
    return f"  {trade.ticker:<5} {'  |  '.join(clauses)}"


def _mfe_event_snapshot_section(trades: Sequence[Trade]) -> list[str]:
    rows = [trade for trade in trades if has_mfe_event_snapshot(trade)]
    if not rows:
        return []
    lines = ["", "MFE / realized R at partial & trail:"]
    lines.extend(format_mfe_event_row(trade) for trade in rows)
    return lines


def trade_fee_hurdle_r(trade: Trade) -> float | None:
    """Persisted fee_hurdle_r from exit_snapshot. Missing or non-numeric is omitted."""
    return snapshot_event_float(trade, "fee_hurdle_r")


def is_fee_hurdle_non_starter(trade: Trade) -> bool | None:
    """True when final MFE_R is below the fee hurdle. None when hurdle data is missing."""
    hurdle = trade_fee_hurdle_r(trade)
    if hurdle is None:
        return None
    return _mfe_r(trade) < hurdle


def _fee_hurdle_strategy_order(names: Iterable[str]) -> list[str]:
    present = set(names)
    ordered = [name for name in COHORT_STRATEGY_NAMES if name in present]
    ordered.extend(sorted(present - set(ordered)))
    return ordered


def fee_hurdle_rows(trades: Sequence[Trade]) -> FeeHurdleSummary:
    """Closed-only fee-hurdle rows for every strategy. Callers pass already-closed trades."""
    rows: list[FeeHurdleRow] = []
    without = 0
    without_by: dict[str, int] = defaultdict(int)
    rows_by: dict[str, list[FeeHurdleRow]] = defaultdict(list)
    for trade in trades:
        name = str(trade.strategy or "")
        hurdle = trade_fee_hurdle_r(trade)
        if hurdle is None:
            without += 1
            without_by[name] += 1
            continue
        mfe = _mfe_r(trade)
        row = FeeHurdleRow(
            ticker=trade.ticker,
            strategy=name,
            opened_at=trade.opened_at,
            hurdle_r=hurdle,
            mfe_r=mfe,
            net_pnl=float(trade.realized_pnl),
            non_starter=mfe < hurdle,
        )
        rows.append(row)
        rows_by[name].append(row)
    by_strategy = tuple(
        FeeHurdleStrategyStats(
            strategy=name,
            with_hurdle=len(rows_by.get(name, ())),
            without_hurdle=without_by.get(name, 0),
            non_starters=sum(1 for row in rows_by.get(name, ()) if row.non_starter),
        )
        for name in _fee_hurdle_strategy_order(set(rows_by) | set(without_by))
    )
    return FeeHurdleSummary(
        rows=tuple(rows),
        with_hurdle=len(rows),
        without_hurdle=without,
        non_starters=sum(1 for row in rows if row.non_starter),
        by_strategy=by_strategy,
    )


def format_fee_hurdle_row(row: FeeHurdleRow) -> str:
    opened = _aware(row.opened_at).astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
    tag = " NON-STARTER" if row.non_starter else ""
    return (
        f"  {row.ticker:<5} {row.strategy:<10} {opened}  hurdle {row.hurdle_r:.2f}R  "
        f"MFE {row.mfe_r:.2f}R  net ${row.net_pnl:+,.2f}{tag}"
    )


def _format_fee_hurdle_strategy_line(stats: FeeHurdleStrategyStats, *, indent: str = "    ") -> str:
    return (
        f"{indent}{stats.strategy}: {stats.non_starters} non-starters of "
        f"{stats.with_hurdle} with hurdle data; {stats.without_hurdle} without"
    )


def _fee_hurdle_section(
    week_trades: Sequence[Trade],
    all_closed: Sequence[Trade],
    *,
    fingerprint_prefix: str = GEN8_CONFIG_FINGERPRINT_PREFIX,
) -> list[str]:
    week = fee_hurdle_rows(week_trades)
    gen8 = fee_hurdle_rows(trades_matching_config_fingerprint(all_closed, fingerprint_prefix))
    if week.with_hurdle + week.without_hurdle == 0 and gen8.with_hurdle + gen8.without_hurdle == 0:
        return []
    lines = [
        "",
        "Fee hurdle vs final MFE (this week):",
        f"  no hurdle data: {week.without_hurdle}",
        f"  non-starters: {week.non_starters} of {week.with_hurdle} with hurdle data "
        "(MFE_R < fee hurdle R)",
    ]
    lines.extend(format_fee_hurdle_row(row) for row in week.rows)
    if week.by_strategy:
        lines.append("  this week by strategy:")
        lines.extend(_format_fee_hurdle_strategy_line(stats) for stats in week.by_strategy)
    lines.append(
        f"  since gen-8 (fp {fingerprint_prefix}): {gen8.non_starters} non-starters of "
        f"{gen8.with_hurdle} with hurdle data; {gen8.without_hurdle} without"
    )
    if gen8.by_strategy:
        lines.append("  since gen-8 by strategy:")
        lines.extend(_format_fee_hurdle_strategy_line(stats) for stats in gen8.by_strategy)
    return lines


def has_shadow_gate_snapshot(trade: Trade) -> bool:
    snapshot = trade.exit_snapshot
    return isinstance(snapshot, dict) and "shadow_gates_version" in snapshot


def shadow_block_flag(trade: Trade, key: str) -> bool | None:
    """True/False from a shadow bool key; None when missing or stored as null."""
    snapshot = trade.exit_snapshot
    if not isinstance(snapshot, dict) or key not in snapshot:
        return None
    value = snapshot[key]
    if value is None:
        return None
    return bool(value)


def _shadow_gate_cohort_stats(trades: Sequence[Trade]) -> ShadowGateCohortStats:
    n = len(trades)
    if n == 0:
        return ShadowGateCohortStats()
    net_pnl = sum(float(trade.realized_pnl) for trade in trades)
    mean_net_r = sum(trade_realized_r(trade) for trade in trades) / n
    return ShadowGateCohortStats(n=n, net_pnl=net_pnl, mean_net_r=mean_net_r)


def _shadow_gate_split(
    trades: Sequence[Trade], flagged: Callable[[Trade], bool]
) -> ShadowGateSplit:
    yes = [trade for trade in trades if flagged(trade)]
    no = [trade for trade in trades if not flagged(trade)]
    return ShadowGateSplit(
        flagged=_shadow_gate_cohort_stats(yes),
        not_flagged=_shadow_gate_cohort_stats(no),
    )


def shadow_gate_summary(trades: Sequence[Trade]) -> ShadowGateSummary:
    """Closed-trade shadow-gate coverage and flagged vs not-flagged splits."""
    with_data: list[Trade] = []
    without = 0
    for trade in trades:
        if has_shadow_gate_snapshot(trade):
            with_data.append(trade)
        else:
            without += 1
    return ShadowGateSummary(
        coverage_n=len(with_data),
        no_shadow_data=without,
        fee_hurdle=_shadow_gate_split(
            with_data, lambda trade: shadow_block_flag(trade, "shadow_block_fee_hurdle") is True
        ),
        cross_sleeve=_shadow_gate_split(
            with_data, lambda trade: shadow_block_flag(trade, "shadow_block_cross_sleeve") is True
        ),
        either=_shadow_gate_split(
            with_data,
            lambda trade: (
                shadow_block_flag(trade, "shadow_block_fee_hurdle") is True
                or shadow_block_flag(trade, "shadow_block_cross_sleeve") is True
            ),
        ),
    )


def format_shadow_gate_split(name: str, split: ShadowGateSplit) -> str:
    flagged = split.flagged
    not_flagged = split.not_flagged
    return (
        f"    {name:<12} flagged n={flagged.n} net ${flagged.net_pnl:+,.2f} "
        f"mean R={flagged.mean_net_r:+.2f}  |  "
        f"not-flagged n={not_flagged.n} net ${not_flagged.net_pnl:+,.2f} "
        f"mean R={not_flagged.mean_net_r:+.2f}"
    )


def format_shadow_gate_summary(summary: ShadowGateSummary, *, label: str) -> list[str]:
    return [
        f"  {label}: coverage n={summary.coverage_n}  no shadow data: {summary.no_shadow_data}",
        format_shadow_gate_split("fee-hurdle", summary.fee_hurdle),
        format_shadow_gate_split("cross-sleeve", summary.cross_sleeve),
        format_shadow_gate_split("either", summary.either),
    ]


def _ops_shadow_fee_hurdle_r_max() -> float:
    try:
        from ..config import get_ops

        return float(get_ops().shadow_gates.fee_hurdle_r_max)
    except Exception:  # noqa: BLE001 - still render the section with the code default
        return 0.5


def _shadow_entry_gates_section(
    week_trades: Sequence[Trade],
    all_closed: Sequence[Trade],
    *,
    fingerprint_prefix: str = GEN8_CONFIG_FINGERPRINT_PREFIX,
    threshold: float | None = None,
) -> list[str]:
    used = _ops_shadow_fee_hurdle_r_max() if threshold is None else float(threshold)
    week = shadow_gate_summary(week_trades)
    gen8 = shadow_gate_summary(trades_matching_config_fingerprint(all_closed, fingerprint_prefix))
    lines = [
        "",
        f"Shadow entry gates (forward-only, no behavior change; threshold {used:.2f} R)",
    ]
    lines.extend(format_shadow_gate_summary(week, label="this week"))
    lines.extend(format_shadow_gate_summary(gen8, label=f"since gen-8 (fp {fingerprint_prefix})"))
    return lines


def intraday_notional_fee_rows(trades: Sequence[Trade]) -> list[IntradayNotionalFee]:
    """Closed-only intraday size vs fee. Callers pass already-closed trades."""
    rows: list[IntradayNotionalFee] = []
    for trade in trades:
        if trade.strategy != "intraday":
            continue
        rows.append(
            IntradayNotionalFee(
                ticker=trade.ticker,
                entry_notional=float(trade.entry_notional or 0.0),
                fees=float(trade.fees_paid or 0.0),
                fee_pct=trade_fee_pct_of_notional(trade),
            )
        )
    return rows


def format_intraday_notional_fee_row(row: IntradayNotionalFee) -> str:
    return (
        f"  {row.ticker:<5} notional ${row.entry_notional:>9,.2f}  "
        f"fees ${row.fees:>8,.2f}  fee% {row.fee_pct:>6.2%}"
    )


def _intraday_notional_fee_section(trades: Sequence[Trade]) -> list[str]:
    rows = intraday_notional_fee_rows(trades)
    if not rows:
        return []
    lines = ["", "Intraday notional vs fee:"]
    lines.extend(format_intraday_notional_fee_row(row) for row in rows)
    if len(rows) > 1:
        notional = sum(row.entry_notional for row in rows)
        fees = sum(row.fees for row in rows)
        fee_pct = (fees / notional) if notional else 0.0
        lines.append(
            format_intraday_notional_fee_row(
                IntradayNotionalFee(
                    ticker="TOTAL",
                    entry_notional=notional,
                    fees=fees,
                    fee_pct=fee_pct,
                )
            )
        )
    return lines


def trade_mfe_capture(trade: Trade, *, epsilon: float = MFE_R_EPSILON) -> float | None:
    """realized_R / MFE_R. None when MFE_R <= epsilon. Not capped at 1.0.

    The weekly capture mean uses ``MFE_CAPTURE_MIN_MFE_R`` via
    ``aggregate_mfe_capture``; this helper keeps the epsilon floor for other
    callers.
    """
    peak = _mfe_r(trade)
    if peak <= epsilon:
        return None
    return trade_realized_r(trade) / peak


def aggregate_mfe_capture(
    trades: Sequence[Trade],
    *,
    epsilon: float = MFE_R_EPSILON,
    min_mfe_r: float = MFE_CAPTURE_MIN_MFE_R,
) -> MfeCaptureStats:
    """Mean/median capture for trades with MFE_R >= ``min_mfe_r``.

    Trades with ``epsilon < MFE_R < min_mfe_r`` count as excluded. Trades with
    MFE_R <= epsilon are omitted from both counts (same as ``trade_mfe_capture``).
    """
    used: list[float] = []
    excluded = 0
    for trade in trades:
        peak = _mfe_r(trade)
        if peak <= epsilon:
            continue
        capture = trade_realized_r(trade) / peak
        if peak < min_mfe_r:
            excluded += 1
            continue
        used.append(capture)
    n = len(used)
    return MfeCaptureStats(
        n=n,
        n_excluded=excluded,
        mean=(sum(used) / n) if n else 0.0,
        median=statistics.median(used) if n else 0.0,
    )


def _wl(wins: int, losses: int, breakeven: int) -> str:
    extra = f" / {breakeven}BE" if breakeven else ""
    return f"{wins}W / {losses}L{extra}"


def _format_fee_to_gross(stats: CostStats) -> str:
    """Display fee/gross as n/a when gross P/L is not positive. Raw ratio stays on CostStats."""
    if stats.gross_pnl <= 0:
        return "n/a"
    return f"{stats.fee_to_gross:>6.2%}"


def _cost_row(
    label: str,
    stats: CostStats,
    label_width: int,
    small_n_threshold: int | None = None,
) -> str:
    line = (
        f"  {label:<{label_width}} {stats.n:>3}  "
        f"{stats.net_win_rate:>4.0%} net  {stats.gross_win_rate:>4.0%} gross  "
        f"gross ${stats.gross_pnl:>9,.2f}  fees ${stats.fees:>8,.2f}  "
        f"net ${stats.net_pnl:>9,.2f}  fee% {stats.fee_pct_of_notional:>6.2%}  "
        f"fee/gross {_format_fee_to_gross(stats)}  "
        f"fees {stats.fee_pct_of_notional:.2%} ntl"
    )
    if small_n_threshold is not None and stats.n < small_n_threshold:
        line += " [small-n]"
    return line


def _merge_cost_stats(rows: Sequence[tuple[str, CostStats]]) -> CostStats:
    return CostStats(
        n=sum(stats.n for _, stats in rows),
        net_wins=sum(stats.net_wins for _, stats in rows),
        net_losses=sum(stats.net_losses for _, stats in rows),
        net_breakeven=sum(stats.net_breakeven for _, stats in rows),
        gross_wins=sum(stats.gross_wins for _, stats in rows),
        gross_losses=sum(stats.gross_losses for _, stats in rows),
        gross_breakeven=sum(stats.gross_breakeven for _, stats in rows),
        gross_pnl=sum(stats.gross_pnl for _, stats in rows),
        fees=sum(stats.fees for _, stats in rows),
        net_pnl=sum(stats.net_pnl for _, stats in rows),
        entry_notional=sum(stats.entry_notional for _, stats in rows),
        hold_seconds=sum(stats.hold_seconds for _, stats in rows),
    )


def _cost_section(
    title: str,
    rows: Sequence[tuple[str, CostStats]],
    small_n_threshold: int | None = None,
) -> list[str]:
    if not rows:
        return []
    width = max(len("TOTAL"), max(len(label) for label, _ in rows), 9)
    lines = ["", title]
    lines.extend(
        _cost_row(label, stats, width, small_n_threshold=small_n_threshold) for label, stats in rows
    )
    if len(rows) > 1:
        lines.append(
            _cost_row(
                "TOTAL",
                _merge_cost_stats(rows),
                width,
                small_n_threshold=small_n_threshold,
            )
        )
    return lines


def _mfe_capture_section(trades: Sequence[Trade]) -> list[str]:
    stats = aggregate_mfe_capture(trades)
    floor = MFE_CAPTURE_MIN_MFE_R
    return [
        "",
        f"MFE capture (realized_R / MFE_R, only MFE_R >= {floor:g}R; "
        f"n={stats.n} used, {stats.n_excluded} excluded): "
        f"mean {stats.mean:.2f}, median {stats.median:.2f}",
    ]


def _closed_trade_digest_sections(
    trades: Sequence[Trade],
    linked_setups: dict[int, str],
    setup_title: str,
    store: Store | None = None,
) -> list[str]:
    if not trades:
        return []
    lines = _cost_section(setup_title, setup_cost_stats(trades, linked_setups))
    lines += _cost_section("By notional:", notional_cost_stats(trades))
    lines += _cost_section("By ticker:", ticker_cost_stats(trades))
    lines += _intraday_notional_fee_section(trades)
    lines += _mfe_capture_section(trades)
    lines += _session_cost_section(trades)
    lines += _entry_slippage_section(trades, store)
    return lines


def _setup_cohort_scoreboard_section(
    *,
    week_trades: Sequence[Trade],
    week_linked: dict[int, str],
    cumulative_trades: Sequence[Trade],
    cumulative_linked: dict[int, str],
    fingerprint_prefix: str = GEN8_CONFIG_FINGERPRINT_PREFIX,
) -> list[str]:
    """This-week vs cumulative CostStats by setup and strategy for one fingerprint prefix."""
    week = trades_matching_config_fingerprint(week_trades, fingerprint_prefix)
    cumulative = trades_matching_config_fingerprint(cumulative_trades, fingerprint_prefix)
    if not week and not cumulative:
        return []
    lines = [
        "",
        f"Setup cohort since gen-8 (fp {fingerprint_prefix}):",
        "[small-n] = fewer than 5 closed trades; treat as anecdotal.",
    ]
    lines += _cost_section(
        "This week:",
        setup_cost_stats(week, week_linked),
        small_n_threshold=SMALL_N_THRESHOLD,
    )
    lines += _cost_section(
        "This week by strategy:",
        strategy_cost_stats(week),
        small_n_threshold=SMALL_N_THRESHOLD,
    )
    lines += _cost_section(
        "Cumulative:",
        setup_cost_stats(cumulative, cumulative_linked),
        small_n_threshold=SMALL_N_THRESHOLD,
    )
    lines += _cost_section(
        "Cumulative by strategy:",
        strategy_cost_stats(cumulative),
        small_n_threshold=SMALL_N_THRESHOLD,
    )
    return lines


def trade_session(trade: Trade) -> str:
    """UTC session name for ``trade.opened_at`` using ``SESSION_BOUNDARIES_UTC``."""
    hour = _aware(trade.opened_at).astimezone(UTC).hour
    for name, start, end in SESSION_BOUNDARIES_UTC:
        if start <= hour < end:
            return name
    return "Unknown"


def session_cost_stats(trades: Sequence[Trade]) -> list[tuple[str, CostStats]]:
    """Always emit the four UTC sessions so counts sum to closed trades."""
    buckets: dict[str, list[Trade]] = {name: [] for name, _, _ in SESSION_BOUNDARIES_UTC}
    extras: dict[str, list[Trade]] = {}
    for trade in trades:
        key = trade_session(trade)
        if key in buckets:
            buckets[key].append(trade)
        else:
            extras.setdefault(key, []).append(trade)
    rows = [(name, aggregate_cost_stats(buckets[name])) for name, _, _ in SESSION_BOUNDARIES_UTC]
    rows.extend((name, aggregate_cost_stats(extras[name])) for name in sorted(extras))
    return rows


def _session_boundaries_header() -> str:
    parts = [
        f"{name} [{start:02d}:00, {end:02d}:00)" for name, start, end in SESSION_BOUNDARIES_UTC
    ]
    return "By entry session (UTC, " + ", ".join(parts) + "):"


def _session_cost_section(trades: Sequence[Trade]) -> list[str]:
    return _cost_section(_session_boundaries_header(), session_cost_stats(trades))


def _effective_close(trade: Trade, horizon: datetime) -> datetime:
    if trade.closed_at is None:
        return horizon
    return _aware(trade.closed_at)


def _intervals_overlap(
    a_open: datetime, a_close: datetime, b_open: datetime, b_close: datetime
) -> bool:
    return a_open < b_close and b_open < a_close


def same_ticker_cross_sleeve_overlaps(
    trades: Sequence[Trade],
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime | None = None,
) -> CrossSleeveOverlapSummary:
    """Groups of same-ticker trades from different strategies with overlapping opens.

    Open trades (``closed_at is None``) stay open until ``now``, or until
    ``window_end`` when that instant is already in the past. A group is kept
    when at least one member closed inside ``[window_start, window_end)``.
    Still-open trades stay in the group.
    """
    start = _aware(window_start)
    end = _aware(window_end)
    if now is not None:
        horizon = _aware(now)
    else:
        clock = datetime.now(UTC)
        horizon = end if end <= clock else clock

    items = list(trades)
    n = len(items)
    if n < 2:
        return CrossSleeveOverlapSummary()

    opens = [_aware(trade.opened_at) for trade in items]
    closes = [_effective_close(trade, horizon) for trade in items]
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    pair_overlaps: list[tuple[int, int, datetime, datetime]] = []
    for i in range(n):
        if closes[i] <= opens[i]:
            continue
        for j in range(i + 1, n):
            if items[i].ticker != items[j].ticker:
                continue
            if str(items[i].strategy or "") == str(items[j].strategy or ""):
                continue
            if closes[j] <= opens[j]:
                continue
            if not _intervals_overlap(opens[i], closes[i], opens[j], closes[j]):
                continue
            overlap_start = max(opens[i], opens[j])
            overlap_end = min(closes[i], closes[j])
            if overlap_end <= overlap_start:
                continue
            union(i, j)
            pair_overlaps.append((i, j, overlap_start, overlap_end))

    clusters: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters[find(i)].append(i)

    pair_by_root: dict[int, list[tuple[datetime, datetime]]] = defaultdict(list)
    for i, _j, overlap_start, overlap_end in pair_overlaps:
        pair_by_root[find(i)].append((overlap_start, overlap_end))

    groups: list[CrossSleeveOverlapGroup] = []
    for root, idxs in clusters.items():
        if len(idxs) < 2 or root not in pair_by_root:
            continue
        members_closed_in_window = False
        members: list[OverlapMember] = []
        closed_net = 0.0
        open_count = 0
        strategies: set[str] = set()
        for i in idxs:
            trade = items[i]
            closed_at = trade.closed_at
            is_open = closed_at is None
            if is_open:
                open_count += 1
                net: float | None = None
            else:
                closed_aware = _aware(closed_at)
                if start <= closed_aware < end:
                    members_closed_in_window = True
                net = float(trade.realized_pnl)
                closed_net += net
            strategies.add(str(trade.strategy or ""))
            members.append(
                OverlapMember(
                    trade_id=getattr(trade, "id", None),
                    strategy=str(trade.strategy or ""),
                    ticker=trade.ticker,
                    opened_at=opens[i],
                    closed_at=None if is_open else _aware(closed_at),
                    net_pnl=net,
                )
            )
        if not members_closed_in_window or len(strategies) < 2:
            continue
        members.sort(key=lambda m: (m.opened_at, m.strategy, m.trade_id or 0))
        span_start = min(pair[0] for pair in pair_by_root[root])
        span_end = max(pair[1] for pair in pair_by_root[root])
        groups.append(
            CrossSleeveOverlapGroup(
                ticker=items[idxs[0]].ticker,
                members=tuple(members),
                overlap_start=span_start,
                overlap_end=span_end,
                combined_net_pnl=closed_net,
                open_count=open_count,
            )
        )

    groups.sort(key=lambda g: (g.ticker, g.overlap_start, g.members[0].trade_id or 0))
    return CrossSleeveOverlapSummary(
        groups=tuple(groups),
        combined_net_pnl=sum(group.combined_net_pnl for group in groups),
    )


def format_cross_sleeve_overlap_group(group: CrossSleeveOverlapGroup) -> str:
    bits: list[str] = []
    for member in group.members:
        ident = f"#{member.trade_id}" if member.trade_id is not None else "#?"
        if member.closed_at is None:
            bits.append(f"{ident} {member.strategy} open")
        else:
            bits.append(f"{ident} {member.strategy} net ${member.net_pnl:+,.2f}")
    start = group.overlap_start.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
    end = group.overlap_end.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
    return (
        f"  {group.ticker:<5} {' + '.join(bits)}  "
        f"overlap {start} – {end}  combined net ${group.combined_net_pnl:+,.2f}"
    )


def format_cross_sleeve_overlap_summary(summary: CrossSleeveOverlapSummary) -> list[str]:
    if not summary.groups:
        return []
    lines = ["", "Same-ticker cross-sleeve overlap:"]
    lines.extend(format_cross_sleeve_overlap_group(group) for group in summary.groups)
    lines.append(
        f"  week combined net of closed members across groups: ${summary.combined_net_pnl:+,.2f}"
    )
    return lines


def _cross_sleeve_overlap_section(
    trades: Sequence[Trade],
    *,
    window_start: datetime,
    window_end: datetime,
    now: datetime | None = None,
) -> list[str]:
    return format_cross_sleeve_overlap_summary(
        same_ticker_cross_sleeve_overlaps(
            trades, window_start=window_start, window_end=window_end, now=now
        )
    )


def _mean_median(values: Sequence[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    return sum(values) / len(values), float(statistics.median(values))


def trade_has_entry_slippage(trade: Trade) -> bool:
    snapshot = trade.exit_snapshot
    if not isinstance(snapshot, dict):
        return False
    return ENTRY_FILL_PRICE_KEY in snapshot or ENTRY_SLIPPAGE_BPS_VS_QUOTE_KEY in snapshot


def aggregate_entry_slippage(trades: Sequence[Trade]) -> EntrySlippageStats:
    """Forward-only fill vs signal / quote from exit_snapshot keys written at open."""
    rows = [trade for trade in trades if trade_has_entry_slippage(trade)]
    if not rows:
        return EntrySlippageStats()
    signal_bps: list[float] = []
    quote_bps: list[float] = []
    signal_usd: list[float] = []
    for trade in rows:
        bps_signal = snapshot_event_float(trade, ENTRY_SLIPPAGE_BPS_VS_SIGNAL_KEY)
        if bps_signal is not None:
            signal_bps.append(bps_signal)
        usd_signal = snapshot_event_float(trade, ENTRY_SLIPPAGE_USD_VS_SIGNAL_KEY)
        if usd_signal is not None:
            signal_usd.append(usd_signal)
        bps_quote = snapshot_event_float(trade, ENTRY_SLIPPAGE_BPS_VS_QUOTE_KEY)
        if bps_quote is not None:
            quote_bps.append(bps_quote)
    mean_sig, med_sig = _mean_median(signal_bps)
    mean_q, med_q = _mean_median(quote_bps)
    return EntrySlippageStats(
        n=len(rows),
        n_signal=len(signal_bps),
        n_quote=len(quote_bps),
        mean_bps_vs_signal=mean_sig,
        median_bps_vs_signal=med_sig,
        mean_bps_vs_quote=mean_q,
        median_bps_vs_quote=med_q,
        total_usd_vs_signal=sum(signal_usd),
    )


def backfill_entry_slippage_vs_proposed(
    trades: Sequence[Trade],
    proposed_prices: dict[int, float],
) -> EntrySlippageStats:
    """Historical fill vs ``opportunity_decisions.proposed_entry_price`` (signal)."""
    signal_bps: list[float] = []
    signal_usd: list[float] = []
    for trade in trades:
        trade_id = getattr(trade, "id", None)
        if trade_id is None or int(trade_id) not in proposed_prices:
            continue
        signal = float(proposed_prices[int(trade_id)])
        fill = float(trade.entry_price or 0.0)
        if signal <= 0 or fill <= 0:
            continue
        qty = float(getattr(trade, "original_qty", 0.0) or trade.qty or 0.0)
        signal_bps.append((fill - signal) / signal * 10_000.0)
        signal_usd.append((fill - signal) * qty)
    n = len(signal_bps)
    mean_sig, med_sig = _mean_median(signal_bps)
    return EntrySlippageStats(
        n=n,
        n_signal=n,
        mean_bps_vs_signal=mean_sig,
        median_bps_vs_signal=med_sig,
        total_usd_vs_signal=sum(signal_usd),
    )


def format_entry_slippage_stats(stats: EntrySlippageStats, *, n_zero_note: str) -> str:
    if stats.n == 0:
        return f"  n=0 ({n_zero_note})"
    return (
        f"  n={stats.n}  mean {stats.mean_bps_vs_signal:.2f} bps vs signal "
        f"(median {stats.median_bps_vs_signal:.2f}, n={stats.n_signal})  "
        f"mean {stats.mean_bps_vs_quote:.2f} bps vs quote "
        f"(median {stats.median_bps_vs_quote:.2f}, n={stats.n_quote})  "
        f"total $ vs signal ${stats.total_usd_vs_signal:+,.2f}"
    )


def _entry_slippage_section(trades: Sequence[Trade], store: Store | None = None) -> list[str]:
    stats = aggregate_entry_slippage(trades)
    lines = [
        "",
        "Entry slippage (forward-only):",
        format_entry_slippage_stats(stats, n_zero_note="forward-only since this ship"),
    ]
    if store is None:
        return lines
    ids = [int(trade.id) for trade in trades if getattr(trade, "id", None)]
    lookup = getattr(store, "proposed_entry_prices_for_trade_ids", None)
    if not callable(lookup) or not ids:
        return lines
    proposed = lookup(ids)
    backfill = backfill_entry_slippage_vs_proposed(trades, proposed)
    if backfill.n == 0:
        lines.append("  backfill (signal=proposed_entry_price): n=0")
        return lines
    lines.append(
        "  backfill (signal=proposed_entry_price): "
        f"n={backfill.n}  mean {backfill.mean_bps_vs_signal:.2f} bps "
        f"(median {backfill.median_bps_vs_signal:.2f})  "
        f"total $ vs signal ${backfill.total_usd_vs_signal:+,.2f}"
    )
    return lines


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
        f"session={trade_session(trade)} "
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
    lines = [
        f"Sold {trade.qty:.8f} {trade.ticker} @ ${trade.exit_price:,.6f}",
        f"Entry: ${trade.entry_price:,.6f}",
        f"Reason: {reason}",
        "",
        f"P/L: ${pnl:+,.2f} ({_pnl_pct(trade):+.2%})",
        f"Fees: ${trade.fees_paid:,.2f}",
    ]
    if is_hard_stop_loss(trade):
        fill = stop_loss_fill_row(trade)
        lines += [
            f"Intended stop: ${fill.intended:,.6f}",
            f"Fill: ${fill.fill:,.6f}",
            f"Δ$: ${fill.delta_dollars:+,.2f}  ΔR {fill.delta_r:+.2f}",
        ]
    lines += [
        f"Held: {_hold_hours(trade):.1f}h",
        f"MFE: {_mfe_r(trade):.2f}R",
        f"Exit profile: {trade.exit_profile_label or 'legacy'}",
        f"Config fingerprint: {trade.config_fingerprint or 'legacy'}",
        f"Exit snapshot: {_snapshot_text(trade)}",
        f"Mode: {'LIVE' if trade.is_live else 'PAPER'}",
    ]
    return subject, "\n".join(lines)


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
    fee_rate: float | None = None,
) -> tuple[str, str]:
    """Subject and body summarizing every trade closed in [start, end)."""
    local_start = start.astimezone(display_tz)
    local_end = end.astimezone(display_tz)

    closed = list(store.closed_trades_between(start, end))
    opened = store.count_trades_opened_between(start, end)
    overall = aggregate_cost_stats(closed)
    linked_setups = store.setup_names_for_trade_ids(trade.id for trade in closed if trade.id)

    # A window ending in the future is the week currently in progress, which the
    # CLI renders on demand; say so rather than implying it is final.
    partial = " (in progress)" if end > datetime.now(UTC) else ""
    subject = (
        f"Weekly report {local_start:%b %d} - {local_end:%b %d}{partial}: ${overall.net_pnl:+,.2f}"
    )

    lines = [
        f"Week of {local_start:%a %b %d, %Y %I:%M %p} to {local_end:%a %b %d, %Y %I:%M %p} "
        f"({local_end:%Z}){partial}",
        f"Mode: {mode}",
        "",
        f"Trades opened:  {opened}",
        f"Trades closed:  {len(closed)}",
        f"Win rate:       {overall.net_win_rate:.0%} "
        f"({_wl(overall.net_wins, overall.net_losses, overall.net_breakeven)}) net  |  "
        f"{overall.gross_win_rate:.0%} "
        f"({_wl(overall.gross_wins, overall.gross_losses, overall.gross_breakeven)}) gross",
        f"Gross P/L:      ${overall.gross_pnl:+,.2f}",
        f"Fees paid:      ${overall.fees:,.2f}",
        f"Fee% of notional: {overall.fee_pct_of_notional:.2%}",
        f"NET P/L:        ${overall.net_pnl:+,.2f}",
        "Costs: gross = realized P/L + fees. Net is realized P/L (already after fees). "
        "Slippage stays in fill price / gross, not the fee line.",
    ]

    if closed:
        lines += ["", "Completed trades:"]
        shown = closed[:max_trades_listed]
        lines += [f"  {format_trade_row(t)}" for t in shown]
        if len(closed) > len(shown):
            lines.append(f"  ... and {len(closed) - len(shown)} more")
    else:
        lines += ["", "No trades closed this week."]

    strategy_names = list(strategies)
    for trade in closed:
        if trade.strategy not in strategy_names:
            strategy_names.append(trade.strategy)
    if strategy_names:
        by_strategy = [
            (name, aggregate_cost_stats([t for t in closed if t.strategy == name]))
            for name in strategy_names
        ]
        lines += _cost_section("By strategy:", by_strategy)
    lines += _closed_trade_digest_sections(closed, linked_setups, "By setup:", store)
    all_closed = list(store.closed_trades())
    cumulative_linked = store.setup_names_for_trade_ids(
        trade.id for trade in all_closed if trade.id
    )
    lines += _setup_cohort_scoreboard_section(
        week_trades=closed,
        week_linked=linked_setups,
        cumulative_trades=all_closed,
        cumulative_linked=cumulative_linked,
    )
    lines += _fee_rate_what_if_section(all_closed, fee_rate)
    lines += _stop_loss_fill_section(closed)
    lines += _stop_loss_fill_rollup_section(closed)
    lines += _mfe_event_snapshot_section(closed)
    lines += _fee_hurdle_section(closed, all_closed)
    lines += _shadow_entry_gates_section(closed, all_closed)

    open_trades = store.open_trades()
    lines += _cross_sleeve_overlap_section(
        [*all_closed, *open_trades],
        window_start=start,
        window_end=end,
    )
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


def _normalize_compare_strategies(
    strategies: Sequence[str | tuple[str, float | None, float | None]],
) -> list[tuple[str, float | None, float | None]]:
    rows: list[tuple[str, float | None, float | None]] = []
    for item in strategies:
        if isinstance(item, str):
            rows.append((item, None, None))
        else:
            name, allocation, alloc_equity = item
            rows.append((name, allocation, alloc_equity))
    return rows


def build_compare_report(
    store: Store,
    strategies: Sequence[str | tuple[str, float | None, float | None]],
    *,
    mode: str = "PAPER",
    fee_rate: float | None = None,
) -> str:
    """Side-by-side strategy costs plus a setup-family split of closed trades."""
    configured = _normalize_compare_strategies(strategies)
    closed = list(store.closed_trades())
    names = [name for name, _, _ in configured]
    for trade in closed:
        if trade.strategy not in names:
            names.append(trade.strategy)
            configured.append((trade.strategy, None, None))

    linked_setups = store.setup_names_for_trade_ids(trade.id for trade in closed if trade.id)
    since = datetime.now(UTC) - timedelta(days=1)

    lines = [
        f"Mode: {mode}  |  Comparing strategies",
        "Costs: gross = realized P/L + fees. Net is realized P/L (already after fees).",
        "Slippage stays in fill price / gross, not the fee line.",
        "Headline win rate is net (realized_pnl > 0); gross win rate uses gross P/L > 0.",
        "",
    ]
    header = (
        f"{'STRATEGY':<10}{'ALLOC':>7}{'ALLOC_EQ':>11}{'OPEN':>6}{'CLOSED':>8}"
        f"{'NET_WR':>8}{'GROSS_WR':>9}{'GROSS':>11}{'FEES':>10}{'NET':>11}"
        f"{'FEE%':>8}{'PNL_24H':>10}{'AVG_HOLD_H':>12}"
    )
    lines += [header, "-" * len(header)]

    by_strategy: list[tuple[str, CostStats]] = []
    for name, allocation, alloc_equity in configured:
        rows = [trade for trade in closed if trade.strategy == name]
        stats = aggregate_cost_stats(rows)
        by_strategy.append((name, stats))
        open_count = store.count_open_trades(name)
        day_pnl = store.realized_pnl_since(since, name)
        alloc_txt = f"{allocation:>6.0%}" if allocation is not None else f"{'n/a':>6}"
        eq_txt = f"{alloc_equity:>11.2f}" if alloc_equity is not None else f"{'n/a':>11}"
        lines.append(
            f"{name:<10}{alloc_txt} {eq_txt}{open_count:>6}{stats.n:>8}"
            f"{stats.net_win_rate:>7.0%} {stats.gross_win_rate:>8.0%} "
            f"{stats.gross_pnl:>10.2f} {stats.fees:>9.2f} {stats.net_pnl:>10.2f}"
            f"{stats.fee_pct_of_notional:>8.2%} {day_pnl:>9.2f}{stats.avg_hold_hours:>12.2f}"
        )

    if len(by_strategy) > 1:
        total = _merge_cost_stats(by_strategy)
        lines.append(
            f"{'TOTAL':<10}{'':>7} {'':>11}{'':>6}{total.n:>8}"
            f"{total.net_win_rate:>7.0%} {total.gross_win_rate:>8.0%} "
            f"{total.gross_pnl:>10.2f} {total.fees:>9.2f} {total.net_pnl:>10.2f}"
            f"{total.fee_pct_of_notional:>8.2%}"
        )
    if closed:
        lines += _closed_trade_digest_sections(
            closed, linked_setups, "By setup (closed trades):", store
        )
        lines += _stop_loss_fill_rollup_section(closed)
    else:
        lines += ["", "No closed trades."]
    week_cutoff = datetime.now(UTC) - timedelta(days=7)
    week_closed = [
        trade
        for trade in closed
        if trade.closed_at is not None and _aware(trade.closed_at) >= week_cutoff
    ]
    week_linked = store.setup_names_for_trade_ids(trade.id for trade in week_closed if trade.id)
    lines += _setup_cohort_scoreboard_section(
        week_trades=week_closed,
        week_linked=week_linked,
        cumulative_trades=closed,
        cumulative_linked=linked_setups,
    )
    lines += _fee_rate_what_if_section(closed, fee_rate)
    return "\n".join(lines)
