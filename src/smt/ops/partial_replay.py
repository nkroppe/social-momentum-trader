"""Report-only post-partial exit replay from ``smt fetch-candles`` CSVs.

Price data is read only from local CSVs. Trade parameters come from the Store
(read-only). No network, no database writes, no policy changes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..backtest import BacktestDataError, _iso, load_candle_csv
from ..config import get_market, get_risk, get_strategies
from ..market.indicators import Candle, atr
from ..models import Trade
from ..store import Store
from ..trader.exit_policy import (
    ResolvedExitProfile,
    first_partial_quantity,
    legacy_profile,
    mfe_r,
    resolve_profile,
)
from .candle_fetch import candle_csv_path
from .reports import (
    GEN8_CONFIG_FINGERPRINT_PREFIX,
    SMALL_N_THRESHOLD,
    trade_realized_r,
    trade_risk_dollars,
    trades_matching_config_fingerprint,
)

SKIP_NO_CSV = "no csv"
SKIP_CSV_ERROR = "csv error"
SKIP_STOPPED_BEFORE_TARGET = "stopped before target"
SKIP_STALE_BEFORE_TARGET = "stale time stop before target"
SKIP_NO_PARTIAL = "no partial in window"
SKIP_ZERO_RISK = "zero risk"

REASON_TRAILING_STOP = "TRAILING_STOP"
REASON_STOP_LOSS = "STOP_LOSS"
REASON_TIME_STOP = "TIME_STOP"
REASON_DATA_END = "DATA_END"

_ATR_TRAIL_MULT = 2.0


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _unix(value: datetime | float | int) -> int:
    if isinstance(value, datetime):
        return int(_aware(value).timestamp())
    return int(value)


def _profile_for(trade: Trade) -> ResolvedExitProfile:
    if trade.exit_snapshot:
        return resolve_profile(trade.exit_snapshot)
    return legacy_profile(trade.strategy)


def _original_qty(trade: Trade) -> float:
    return float(trade.original_qty or 0.0) or float(trade.qty or 0.0)


def _stop_reason(stop: float, stop_loss: float) -> str:
    return REASON_TRAILING_STOP if stop > stop_loss else REASON_STOP_LOSS


def fee_rate_for_strategy(
    strategy: str,
    *,
    rates: Mapping[str, float] | None = None,
    default_rate: float | None = None,
) -> float:
    """Per-side fee: strategy config by name, else risk ``assumed_fee_pct_per_side``."""
    if rates is not None and strategy in rates:
        return float(rates[strategy])
    if default_rate is not None:
        return float(default_rate)
    bundle = get_strategies()
    cfg = bundle.strategies.get(strategy)
    if cfg is not None:
        return float(cfg.assumed_fee_pct_per_side)
    return float(get_risk().assumed_fee_pct_per_side)


def realized_r_after_partial(
    *,
    entry: float,
    take_profit: float,
    exit_price: float,
    qty_p: float,
    remaining: float,
    original_qty: float,
    fee_rate: float,
    initial_risk_per_unit: float,
) -> float:
    """Net P/L in R after a first partial plus remaining-qty exit."""
    risk = initial_risk_per_unit * original_qty
    if risk <= 0:
        return 0.0
    fees = fee_rate * (entry * original_qty + take_profit * qty_p + exit_price * remaining)
    net = (take_profit - entry) * qty_p + (exit_price - entry) * remaining - fees
    return net / risk


def breakeven_plus_fees_stop(entry_price: float, stop_loss: float, fee_rate: float) -> float:
    """Static remaining stop: ``max(stop_loss, entry + entry_fee + est exit fee)``."""
    per_unit = entry_price * fee_rate + entry_price * fee_rate
    return max(stop_loss, entry_price + per_unit)


@dataclass(frozen=True)
class VariantExit:
    price: float
    reason: str
    data_end: bool


@dataclass(frozen=True)
class TradeReplay:
    """Pure replay of one trade that reached a first partial."""

    partial_ts: int
    qty_p: float
    remaining: float
    b: VariantExit
    c: VariantExit
    b_r: float
    c_r: float


@dataclass(frozen=True)
class PartialReplayRow:
    trade_id: int
    ticker: str
    strategy: str
    opened_at: datetime
    partial_bar_time: datetime
    actual_r: float
    b_r: float
    b_reason: str
    c_r: float
    c_reason: str
    stored_partial: bool
    data_end: bool


@dataclass(frozen=True)
class SkippedTrade:
    trade_id: int
    ticker: str
    reason: str


@dataclass(frozen=True)
class PartialReplayResult:
    rows: tuple[PartialReplayRow, ...]
    skipped: tuple[SkippedTrade, ...]
    mean_actual_r: float
    mean_b_r: float
    mean_c_r: float
    n: int
    small_n: bool


def _entry_start_index(
    candles: Sequence[Candle], granularity: int, opened_ts: int
) -> int | None:
    for i, candle in enumerate(candles):
        if candle.ts + granularity > opened_ts:
            return i
    return None


def _hit_time_stop(
    candle: Candle, granularity: int, opened_ts: int, time_stop_hours: int
) -> bool:
    return candle.ts + granularity >= opened_ts + int(time_stop_hours) * 3600


def find_partial_bar(
    candles: Sequence[Candle],
    granularity: int,
    opened_at: datetime | float | int,
    stop_loss: float,
    take_profit: float,
    profile: ResolvedExitProfile,
    entry_price: float,
    initial_risk_per_unit: float,
) -> int | str:
    """Index of the first bar whose high reaches TP, or a skip reason."""
    opened_ts = _unix(opened_at)
    start = _entry_start_index(candles, granularity, opened_ts)
    if start is None:
        return SKIP_NO_PARTIAL
    highest = entry_price
    for i in range(start, len(candles)):
        bar = candles[i]
        is_entry = i == start
        if not is_entry and bar.low <= stop_loss:
            return SKIP_STOPPED_BEFORE_TARGET
        if bar.high >= take_profit:
            return i
        highest = max(highest, bar.high)
        if (
            profile.advanced_exit_enabled
            and _hit_time_stop(bar, granularity, opened_ts, profile.stale_time_stop_hours)
            and mfe_r(highest, entry_price, initial_risk_per_unit) < profile.stale_mfe_r
        ):
            return SKIP_STALE_BEFORE_TARGET
        if _hit_time_stop(bar, granularity, opened_ts, profile.time_stop_hours):
            return SKIP_NO_PARTIAL
    return SKIP_NO_PARTIAL


def _atr_trail_stop(
    candles: Sequence[Candle],
    up_to: int,
    atr_periods: int,
    prev_stop: float,
    stop_loss: float,
    highest: float,
) -> float:
    atr_abs = atr(list(candles[: up_to + 1]), atr_periods)
    return max(prev_stop, stop_loss, highest - _ATR_TRAIL_MULT * atr_abs)


def walk_post_partial(
    candles: Sequence[Candle],
    granularity: int,
    partial_index: int,
    opened_at: datetime | float | int,
    time_stop_hours: int,
    stop_loss: float,
    take_profit: float,
    entry_price: float,
    fee_rate: float,
    atr_periods: int,
    variant: str,
) -> VariantExit:
    """Walk bars after the partial. ``variant`` is ``"b"`` (BE+fees) or ``"c"`` (2.0x ATR)."""
    partial_bar = candles[partial_index]
    highest = max(take_profit, partial_bar.high)
    if variant == "b":
        stop = breakeven_plus_fees_stop(entry_price, stop_loss, fee_rate)
    else:
        stop = _atr_trail_stop(
            candles, partial_index, atr_periods, stop_loss, stop_loss, highest
        )
    opened_ts = _unix(opened_at)
    last_close = partial_bar.close
    for i in range(partial_index + 1, len(candles)):
        bar = candles[i]
        last_close = bar.close
        if bar.low <= stop:
            return VariantExit(min(bar.open, stop), _stop_reason(stop, stop_loss), False)
        highest = max(highest, bar.high)
        if variant == "c":
            stop = _atr_trail_stop(candles, i, atr_periods, stop, stop_loss, highest)
        if _hit_time_stop(bar, granularity, opened_ts, time_stop_hours):
            return VariantExit(bar.close, REASON_TIME_STOP, False)
    return VariantExit(last_close, REASON_DATA_END, True)


def replay_trade(
    *,
    opened_at: datetime | float | int,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    original_qty: float,
    initial_risk_per_unit: float,
    candles: Sequence[Candle],
    granularity: int,
    profile: ResolvedExitProfile,
    atr_periods: int,
    fee_rate: float,
) -> TradeReplay | str:
    """Replay variants (b) and (c) for one trade, or a skip reason."""
    found = find_partial_bar(
        candles,
        granularity,
        opened_at,
        stop_loss,
        take_profit,
        profile,
        entry_price,
        initial_risk_per_unit,
    )
    if isinstance(found, str):
        return found
    qty_p = first_partial_quantity(
        original_qty, original_qty, profile.partial_take_profit_fraction
    )
    remaining = original_qty - qty_p
    b_exit = walk_post_partial(
        candles,
        granularity,
        found,
        opened_at,
        profile.time_stop_hours,
        stop_loss,
        take_profit,
        entry_price,
        fee_rate,
        atr_periods,
        "b",
    )
    c_exit = walk_post_partial(
        candles,
        granularity,
        found,
        opened_at,
        profile.time_stop_hours,
        stop_loss,
        take_profit,
        entry_price,
        fee_rate,
        atr_periods,
        "c",
    )
    kwargs = dict(
        entry=entry_price,
        take_profit=take_profit,
        qty_p=qty_p,
        remaining=remaining,
        original_qty=original_qty,
        fee_rate=fee_rate,
        initial_risk_per_unit=initial_risk_per_unit,
    )
    return TradeReplay(
        partial_ts=candles[found].ts,
        qty_p=qty_p,
        remaining=remaining,
        b=b_exit,
        c=c_exit,
        b_r=realized_r_after_partial(exit_price=b_exit.price, **kwargs),
        c_r=realized_r_after_partial(exit_price=c_exit.price, **kwargs),
    )


def _mean(values: Sequence[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def run_partial_replay(
    store: Store,
    candles_dir: Path,
    *,
    fingerprint_prefix: str = GEN8_CONFIG_FINGERPRINT_PREFIX,
    atr_periods: int | None = None,
    fee_rates: Mapping[str, float] | None = None,
    default_fee_rate: float | None = None,
) -> PartialReplayResult:
    """Replay closed GEN8-prefix trades against local fetch-candles CSVs."""
    periods = get_market().atr_periods if atr_periods is None else atr_periods
    matched = trades_matching_config_fingerprint(store.closed_trades(), fingerprint_prefix)
    rows: list[PartialReplayRow] = []
    skipped: list[SkippedTrade] = []
    candles_dir = Path(candles_dir)

    for trade in matched:
        trade_id = int(trade.id or 0)
        original_qty = _original_qty(trade)
        if original_qty <= 0 or trade_risk_dollars(trade) <= 0:
            skipped.append(SkippedTrade(trade_id, trade.ticker, SKIP_ZERO_RISK))
            continue
        path = candle_csv_path(candles_dir, trade)
        if not path.exists():
            skipped.append(SkippedTrade(trade_id, trade.ticker, SKIP_NO_CSV))
            continue
        try:
            candles, granularity = load_candle_csv(path)
        except (BacktestDataError, OSError, ValueError):
            skipped.append(SkippedTrade(trade_id, trade.ticker, SKIP_CSV_ERROR))
            continue
        profile = _profile_for(trade)
        fee_rate = fee_rate_for_strategy(
            trade.strategy, rates=fee_rates, default_rate=default_fee_rate
        )
        replayed = replay_trade(
            opened_at=trade.opened_at,
            entry_price=float(trade.entry_price),
            stop_loss=float(trade.stop_loss),
            take_profit=float(trade.take_profit),
            original_qty=original_qty,
            initial_risk_per_unit=float(trade.initial_risk_per_unit or 0.0),
            candles=candles,
            granularity=granularity,
            profile=profile,
            atr_periods=periods,
            fee_rate=fee_rate,
        )
        if isinstance(replayed, str):
            skipped.append(SkippedTrade(trade_id, trade.ticker, replayed))
            continue
        rows.append(
            PartialReplayRow(
                trade_id=trade_id,
                ticker=trade.ticker,
                strategy=trade.strategy,
                opened_at=_aware(trade.opened_at),
                partial_bar_time=datetime.fromtimestamp(replayed.partial_ts, UTC),
                actual_r=trade_realized_r(trade),
                b_r=replayed.b_r,
                b_reason=replayed.b.reason,
                c_r=replayed.c_r,
                c_reason=replayed.c.reason,
                stored_partial=bool(trade.partial_taken),
                data_end=replayed.b.data_end or replayed.c.data_end,
            )
        )

    n = len(rows)
    return PartialReplayResult(
        rows=tuple(rows),
        skipped=tuple(skipped),
        mean_actual_r=_mean([row.actual_r for row in rows]),
        mean_b_r=_mean([row.b_r for row in rows]),
        mean_c_r=_mean([row.c_r for row in rows]),
        n=n,
        small_n=n < SMALL_N_THRESHOLD,
    )


def _yn(value: bool) -> str:
    return "Y" if value else "N"


def format_partial_replay(result: PartialReplayResult) -> str:
    """Plain-text report: assumption header, fixed-width table, means, skips."""
    lines = [
        "Candle granularity equals the trade trail granularity (CSV from fetch-candles).",
        "Stop-first ambiguity: a bar that touches the stop and a new high fills the stop first.",
        "Stale time stop before target: a pre-partial trade whose MFE stays below "
        "stale_mfe_r through stale_time_stop_hours is skipped.",
        "Variant b: remaining stop is max(stop_loss, entry + 2 * entry * fee_rate), "
        "static after the partial.",
        "Variant c: remaining stop is max(prev_stop, stop_loss, highest - 2.0 * ATR) "
        "on CSV trail bars.",
        "Fee rate is StrategyConfig.assumed_fee_pct_per_side by name, "
        "else risk.assumed_fee_pct_per_side.",
        "",
    ]
    header = (
        f"{'id':>5} {'ticker':<6} {'strategy':<10} {'opened_at':<20} {'partial_at':<20} "
        f"{'actual':>8} {'b_r':>8} {'b_reason':<14} {'c_r':>8} {'c_reason':<14} "
        f"{'stored':<6} {'data_end':<8}"
    )
    lines.append(header)
    for row in result.rows:
        lines.append(
            f"{row.trade_id:>5} {row.ticker:<6} {row.strategy:<10} "
            f"{_iso(_unix(row.opened_at)):<20} {_iso(_unix(row.partial_bar_time)):<20} "
            f"{row.actual_r:>8.3f} {row.b_r:>8.3f} {row.b_reason:<14} "
            f"{row.c_r:>8.3f} {row.c_reason:<14} {_yn(row.stored_partial):<6} "
            f"{_yn(row.data_end):<8}"
        )
    lines.append(
        f"{'MEAN':>5} {'':<6} {'':<10} {'':<20} {'':<20} "
        f"{result.mean_actual_r:>8.3f} {result.mean_b_r:>8.3f} {'':<14} "
        f"{result.mean_c_r:>8.3f} {'':<14} {'':<6} {'':<8}"
    )
    n_line = f"n={result.n}"
    if result.small_n:
        n_line += " [small-n]"
    lines.append(n_line)
    if result.skipped:
        lines.append("skipped:")
        for item in result.skipped:
            lines.append(f"  trade={item.trade_id} {item.ticker}: {item.reason}")
        counts = Counter(item.reason for item in result.skipped)
        tally = ", ".join(f"{reason}={counts[reason]}" for reason in sorted(counts))
        lines.append(f"skipped counts: {tally}")
    return "\n".join(lines) + "\n"
