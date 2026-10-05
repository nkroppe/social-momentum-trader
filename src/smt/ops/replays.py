"""Retrospective read-only replays of closed gen-8 trades.

Report and CLI only. Reads the Store; never writes; does not change trading
behavior.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from ..models import Trade
from ..store import Store
from ..trader.exit_policy import fee_hurdle_r
from .reports import (
    COHORT_STRATEGY_NAMES,
    GEN8_CONFIG_FINGERPRINT_PREFIX,
    SETUP_BUCKETS,
    SMALL_N_THRESHOLD,
    resolve_setup_name,
    snapshot_event_float,
    strategy_cost_stats,
    trade_fee_legs_notional,
    trade_gross_pnl,
    trade_net_at_fee_rate,
    trade_realized_r,
    trades_matching_config_fingerprint,
)

DEFAULT_FEE_RATES = (0.004, 0.006, 0.009)
DEFAULT_FEE_RATES_TEXT = "0.004,0.006,0.009"
DEFAULT_FEE_PCT_PER_SIDE = 0.006
DEFAULT_FEE_HURDLE_R_MAX = 0.5
COMPARE_SETUPS = ("breakout_retest", "breakout_close")

HURDLE_STORED = "stored"
HURDLE_RECOMPUTED = "recomputed"
HURDLE_UNAVAILABLE = "unavailable"

GATE_FEE_HURDLE = "fee-hurdle"
GATE_CROSS_SLEEVE = "cross-sleeve"
GATE_UNION = "union"

FEE_SENSITIVITY_HEADER = "Fee sensitivity"
RETRO_GATES_HEADER = "Retroactive I4 gates"
SETUP_BY_STRATEGY_HEADER = "breakout_retest vs breakout_close by strategy"


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _usd(value: float) -> str:
    return f"${value:+,.2f}"


def _mean_r(trades: Sequence[Trade]) -> float:
    n = len(trades)
    return (sum(trade_realized_r(trade) for trade in trades) / n) if n else 0.0


def _net(trades: Sequence[Trade]) -> float:
    return sum(float(trade.realized_pnl) for trade in trades)


def _linked(linked_setups: Mapping[int, str] | None) -> dict[int, str]:
    return dict(linked_setups or {})


def trade_qty_for_hurdle(trade: Trade) -> float:
    """original_qty when set, otherwise qty — same fallback as fee_hurdle_r at entry."""
    return float(trade.original_qty or 0.0) or float(trade.qty or 0.0)


def strategy_assumed_fee_pct_per_side(
    strategy: str,
    *,
    rates: Mapping[str, float] | None = None,
    default_rate: float = DEFAULT_FEE_PCT_PER_SIDE,
) -> float:
    """Read-only per-side fee: injected map, else StrategyConfig, else 0.006."""
    if rates is not None:
        if strategy in rates:
            return float(rates[strategy])
        return float(default_rate)
    try:
        from ..config import get_strategies

        cfg = get_strategies().strategies.get(strategy)
        if cfg is not None:
            return float(cfg.assumed_fee_pct_per_side)
    except Exception:  # noqa: BLE001 - report still renders with the code default
        pass
    return float(default_rate)


def resolve_fee_hurdle(
    trade: Trade,
    *,
    strategy_fee_rates: Mapping[str, float] | None = None,
    default_fee_pct_per_side: float = DEFAULT_FEE_PCT_PER_SIDE,
) -> tuple[float | None, str]:
    """Stored exit_snapshot fee_hurdle_r, else recompute, else unavailable."""
    stored = snapshot_event_float(trade, "fee_hurdle_r")
    if stored is not None:
        return stored, HURDLE_STORED
    pct = snapshot_event_float(trade, "fee_hurdle_pct_per_side")
    if pct is None:
        pct = strategy_assumed_fee_pct_per_side(
            str(trade.strategy or ""),
            rates=strategy_fee_rates,
            default_rate=default_fee_pct_per_side,
        )
    hurdle = fee_hurdle_r(
        float(trade.entry_price or 0.0),
        trade_qty_for_hurdle(trade),
        float(trade.initial_risk_per_unit or 0.0),
        float(pct),
    )
    if hurdle is None:
        return None, HURDLE_UNAVAILABLE
    return hurdle, HURDLE_RECOMPUTED


def would_block_fee_hurdle(hurdle: float | None, threshold: float) -> bool:
    """True when hurdle exceeds OpsConfig.shadow_gates.fee_hurdle_r_max (I4)."""
    return hurdle is not None and hurdle > threshold


def would_block_cross_sleeve(trade: Trade, holders: Sequence[Trade]) -> bool:
    """True when another strategy already held the same ticker at opened_at.

    A holder is any store trade (any fingerprint, open or closed) with
    opened_at < this.opened_at and (closed_at is None or closed_at > this.opened_at).
    """
    opened = _aware(trade.opened_at)
    trade_id = getattr(trade, "id", None)
    for other in holders:
        if other is trade:
            continue
        other_id = getattr(other, "id", None)
        if trade_id is not None and other_id is not None and trade_id == other_id:
            continue
        if other.ticker != trade.ticker:
            continue
        if other.strategy == trade.strategy:
            continue
        if _aware(other.opened_at) >= opened:
            continue
        closed = other.closed_at
        if closed is not None and _aware(closed) <= opened:
            continue
        return True
    return False


def break_even_fee_rate(trades: Sequence[Trade]) -> float | None:
    """Per-side rate r where gross - r * fee_legs_notional = 0. None if gross <= 0."""
    gross = sum(trade_gross_pnl(trade) for trade in trades)
    legs = sum(trade_fee_legs_notional(trade) for trade in trades)
    if gross <= 0 or legs <= 0:
        return None
    return gross / legs


@dataclass(frozen=True)
class FeeSensitivityRow:
    label: str
    group: str
    n: int
    gross: float
    actual_fees: float
    actual_net: float
    fees_at_rate: tuple[float, ...]
    net_at_rate: tuple[float, ...]


@dataclass(frozen=True)
class FeeSensitivityTable:
    fee_rates: tuple[float, ...]
    rows: tuple[FeeSensitivityRow, ...]
    break_even_fee_rate: float | None


@dataclass(frozen=True)
class GateCoverage:
    stored: int = 0
    recomputed: int = 0
    unavailable: int = 0

    @property
    def n(self) -> int:
        return self.stored + self.recomputed + self.unavailable


@dataclass(frozen=True)
class GateSplitRow:
    name: str
    blocked_n: int
    blocked_net: float
    blocked_mean_r: float
    kept_n: int
    kept_net: float
    kept_mean_r: float
    total: int


@dataclass(frozen=True)
class RetroGatesTable:
    fee_hurdle_r_max: float
    coverage: GateCoverage
    rows: tuple[GateSplitRow, ...]


@dataclass(frozen=True)
class SetupStrategyRow:
    setup: str
    strategy: str
    n: int
    gross: float
    fees: float
    net: float
    mean_net_r: float
    net_win_pct: float
    small_n: bool


@dataclass(frozen=True)
class SetupStrategyTable:
    rows: tuple[SetupStrategyRow, ...]


@dataclass(frozen=True)
class ReplaysResult:
    fingerprint_prefix: str
    n: int
    fee_sensitivity: FeeSensitivityTable
    retro_gates: RetroGatesTable
    setup_by_strategy: SetupStrategyTable


def _fee_sensitivity_row(
    label: str, group: str, trades: Sequence[Trade], rates: Sequence[float]
) -> FeeSensitivityRow:
    return FeeSensitivityRow(
        label=label,
        group=group,
        n=len(trades),
        gross=sum(trade_gross_pnl(trade) for trade in trades),
        actual_fees=sum(float(trade.fees_paid) for trade in trades),
        actual_net=_net(trades),
        fees_at_rate=tuple(
            sum(trade_fee_legs_notional(trade) * rate for trade in trades) for rate in rates
        ),
        net_at_rate=tuple(
            sum(trade_net_at_fee_rate(trade, rate) for trade in trades) for rate in rates
        ),
    )


def build_fee_sensitivity(
    trades: Sequence[Trade],
    linked_setups: Mapping[int, str] | None,
    fee_rates: Sequence[float],
) -> FeeSensitivityTable:
    """All, then by setup, then by strategy. What-if nets reuse reports fee helpers."""
    rates = tuple(float(rate) for rate in fee_rates)
    linked = _linked(linked_setups)
    rows = [_fee_sensitivity_row("All", "all", trades, rates)]
    by_setup: dict[str, list[Trade]] = {name: [] for name in SETUP_BUCKETS}
    for trade in trades:
        by_setup[resolve_setup_name(trade, linked)].append(trade)
    for name in SETUP_BUCKETS:
        rows.append(_fee_sensitivity_row(name, "setup", by_setup[name], rates))
    for name, _stats in strategy_cost_stats(trades):
        bucket = [trade for trade in trades if str(trade.strategy or "") == name]
        rows.append(_fee_sensitivity_row(name, "strategy", bucket, rates))
    return FeeSensitivityTable(
        fee_rates=rates,
        rows=tuple(rows),
        break_even_fee_rate=break_even_fee_rate(trades),
    )


def _gate_split_row(name: str, blocked: Sequence[Trade], kept: Sequence[Trade]) -> GateSplitRow:
    return GateSplitRow(
        name=name,
        blocked_n=len(blocked),
        blocked_net=_net(blocked),
        blocked_mean_r=_mean_r(blocked),
        kept_n=len(kept),
        kept_net=_net(kept),
        kept_mean_r=_mean_r(kept),
        total=len(blocked) + len(kept),
    )


def _partition(trades: Sequence[Trade], blocked: Sequence[bool]) -> tuple[list[Trade], list[Trade]]:
    yes = [trade for trade, flag in zip(trades, blocked, strict=True) if flag]
    no = [trade for trade, flag in zip(trades, blocked, strict=True) if not flag]
    return yes, no


def build_retro_gates(
    trades: Sequence[Trade],
    holders: Sequence[Trade],
    *,
    fee_hurdle_r_max: float = DEFAULT_FEE_HURDLE_R_MAX,
    strategy_fee_rates: Mapping[str, float] | None = None,
    default_fee_pct_per_side: float = DEFAULT_FEE_PCT_PER_SIDE,
) -> RetroGatesTable:
    """Apply I4 shadow-gate rules retroactively to a closed-trade cohort."""
    threshold = float(fee_hurdle_r_max)
    stored = recomputed = unavailable = 0
    fee_flags: list[bool] = []
    cross_flags: list[bool] = []
    for trade in trades:
        hurdle, source = resolve_fee_hurdle(
            trade,
            strategy_fee_rates=strategy_fee_rates,
            default_fee_pct_per_side=default_fee_pct_per_side,
        )
        if source == HURDLE_STORED:
            stored += 1
        elif source == HURDLE_RECOMPUTED:
            recomputed += 1
        else:
            unavailable += 1
        fee_flags.append(would_block_fee_hurdle(hurdle, threshold))
        cross_flags.append(would_block_cross_sleeve(trade, holders))
    union_flags = [fee or cross for fee, cross in zip(fee_flags, cross_flags, strict=True)]
    fee_blocked, fee_kept = _partition(trades, fee_flags)
    cross_blocked, cross_kept = _partition(trades, cross_flags)
    union_blocked, union_kept = _partition(trades, union_flags)
    return RetroGatesTable(
        fee_hurdle_r_max=threshold,
        coverage=GateCoverage(stored=stored, recomputed=recomputed, unavailable=unavailable),
        rows=(
            _gate_split_row(GATE_FEE_HURDLE, fee_blocked, fee_kept),
            _gate_split_row(GATE_CROSS_SLEEVE, cross_blocked, cross_kept),
            _gate_split_row(GATE_UNION, union_blocked, union_kept),
        ),
    )


def _setup_strategy_row(setup: str, strategy: str, trades: Sequence[Trade]) -> SetupStrategyRow:
    n = len(trades)
    net_wins = sum(1 for trade in trades if float(trade.realized_pnl) > 0)
    return SetupStrategyRow(
        setup=setup,
        strategy=strategy,
        n=n,
        gross=sum(trade_gross_pnl(trade) for trade in trades),
        fees=sum(float(trade.fees_paid) for trade in trades),
        net=_net(trades),
        mean_net_r=_mean_r(trades),
        net_win_pct=(net_wins / n) if n else 0.0,
        small_n=n < SMALL_N_THRESHOLD,
    )


def build_setup_by_strategy(
    trades: Sequence[Trade],
    linked_setups: Mapping[int, str] | None,
) -> SetupStrategyTable:
    """breakout_retest vs breakout_close crossed with strategy."""
    linked = _linked(linked_setups)
    buckets: dict[tuple[str, str], list[Trade]] = defaultdict(list)
    strategies: set[str] = set()
    for trade in trades:
        setup = resolve_setup_name(trade, linked)
        if setup not in COMPARE_SETUPS:
            continue
        name = str(trade.strategy or "")
        buckets[(setup, name)].append(trade)
        strategies.add(name)
    ordered = [name for name in COHORT_STRATEGY_NAMES if name in strategies]
    ordered.extend(sorted(strategies - set(COHORT_STRATEGY_NAMES)))
    rows = [
        _setup_strategy_row(setup, name, buckets.get((setup, name), []))
        for setup in COMPARE_SETUPS
        for name in ordered
    ]
    return SetupStrategyTable(rows=tuple(rows))


def _fee_hurdle_r_max(override: float | None) -> float:
    if override is not None:
        return float(override)
    try:
        from ..config import get_ops

        return float(get_ops().shadow_gates.fee_hurdle_r_max)
    except Exception:  # noqa: BLE001 - still render with the code default
        return DEFAULT_FEE_HURDLE_R_MAX


def run_replays(
    store: Store,
    *,
    fingerprint_prefix: str = GEN8_CONFIG_FINGERPRINT_PREFIX,
    fee_rates: Sequence[float] = DEFAULT_FEE_RATES,
    fee_hurdle_r_max: float | None = None,
    strategy_fee_rates: Mapping[str, float] | None = None,
    default_fee_pct_per_side: float = DEFAULT_FEE_PCT_PER_SIDE,
) -> ReplaysResult:
    """Closed trades matching ``fingerprint_prefix``. Store is read-only."""
    closed = list(store.closed_trades())
    matched = trades_matching_config_fingerprint(closed, fingerprint_prefix)
    linked = store.setup_names_for_trade_ids(trade.id for trade in matched if trade.id)
    holders = list(store.open_trades()) + closed
    rates = tuple(float(rate) for rate in fee_rates)
    return ReplaysResult(
        fingerprint_prefix=fingerprint_prefix,
        n=len(matched),
        fee_sensitivity=build_fee_sensitivity(matched, linked, rates),
        retro_gates=build_retro_gates(
            matched,
            holders,
            fee_hurdle_r_max=_fee_hurdle_r_max(fee_hurdle_r_max),
            strategy_fee_rates=strategy_fee_rates,
            default_fee_pct_per_side=default_fee_pct_per_side,
        ),
        setup_by_strategy=build_setup_by_strategy(matched, linked),
    )


def format_fee_sensitivity(table: FeeSensitivityTable, *, fingerprint_prefix: str, n: int) -> str:
    rates = table.fee_rates
    rate_headers = []
    for rate in rates:
        label = f"{rate:.2%}"
        rate_headers.append(f"fees@{label}")
        rate_headers.append(f"net@{label}")
    header_cells = ["group", "label", "n", "gross", *rate_headers, "fees", "net"]
    body: list[list[str]] = []
    for row in table.rows:
        cells = [row.group, row.label, str(row.n), _usd(row.gross)]
        for fees, net in zip(row.fees_at_rate, row.net_at_rate, strict=True):
            cells.append(_usd(fees))
            cells.append(_usd(net))
        cells.append(_usd(row.actual_fees))
        cells.append(_usd(row.actual_net))
        body.append(cells)
    widths = [len(name) for name in header_cells]
    for cells in body:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
    # numeric columns right-aligned after group/label
    aligns = ["<"] * 2 + [">"] * (len(header_cells) - 2)

    def _fmt(cells: Sequence[str]) -> str:
        parts = [f"{cell:{aligns[i]}{widths[i]}}" for i, cell in enumerate(cells)]
        return "  " + "  ".join(parts)

    be = table.break_even_fee_rate
    be_text = f"{be:.2%}" if be is not None else "n/a"
    lines = [
        f"{FEE_SENSITIVITY_HEADER} (per-side; fp {fingerprint_prefix}; n={n})",
        _fmt(header_cells),
        *[_fmt(cells) for cells in body],
        f"  Break-even fee rate per side (All): {be_text}",
    ]
    return "\n".join(lines)


def format_retro_gates(table: RetroGatesTable) -> str:
    cov = table.coverage
    header_cells = [
        "gate",
        "blocked n",
        "blocked net",
        "blocked mean R",
        "kept n",
        "kept net",
        "kept mean R",
        "total",
    ]
    body: list[list[str]] = []
    for row in table.rows:
        body.append(
            [
                row.name,
                str(row.blocked_n),
                _usd(row.blocked_net),
                f"{row.blocked_mean_r:+.2f}",
                str(row.kept_n),
                _usd(row.kept_net),
                f"{row.kept_mean_r:+.2f}",
                str(row.total),
            ]
        )
    widths = [len(name) for name in header_cells]
    for cells in body:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
    aligns = ["<"] + [">"] * (len(header_cells) - 1)

    def _fmt(cells: Sequence[str]) -> str:
        parts = [f"{cell:{aligns[i]}{widths[i]}}" for i, cell in enumerate(cells)]
        return "  " + "  ".join(parts)

    lines = [
        (f"{RETRO_GATES_HEADER} (threshold {table.fee_hurdle_r_max:.2f} R; no behavior change)"),
        (
            f"  coverage: stored={cov.stored}  recomputed={cov.recomputed}  "
            f"unavailable={cov.unavailable}  n={cov.n}"
        ),
        _fmt(header_cells),
        *[_fmt(cells) for cells in body],
    ]
    return "\n".join(lines)


def format_setup_by_strategy(table: SetupStrategyTable) -> str:
    header_cells = ["setup", "strategy", "n", "gross", "fees", "net", "mean R", "net win%"]
    body: list[list[str]] = []
    tags: list[str] = []
    for row in table.rows:
        body.append(
            [
                row.setup,
                row.strategy,
                str(row.n),
                _usd(row.gross),
                _usd(row.fees),
                _usd(row.net),
                f"{row.mean_net_r:+.2f}",
                f"{row.net_win_pct:.0%}",
            ]
        )
        tags.append(" [small-n]" if row.small_n else "")
    widths = [len(name) for name in header_cells]
    for cells in body:
        for i, cell in enumerate(cells):
            widths[i] = max(widths[i], len(cell))
    aligns = ["<", "<", ">", ">", ">", ">", ">", ">"]

    def _fmt(cells: Sequence[str], tag: str = "") -> str:
        parts = [f"{cell:{aligns[i]}{widths[i]}}" for i, cell in enumerate(cells)]
        return "  " + "  ".join(parts) + tag

    lines = [
        SETUP_BY_STRATEGY_HEADER,
        _fmt(header_cells),
        *[_fmt(cells, tag) for cells, tag in zip(body, tags, strict=True)],
    ]
    return "\n".join(lines)


def format_replays(result: ReplaysResult) -> str:
    """Plain-text three-table report. Trailing newline for CLI print(end='')."""
    sections = [
        format_fee_sensitivity(
            result.fee_sensitivity,
            fingerprint_prefix=result.fingerprint_prefix,
            n=result.n,
        ),
        format_retro_gates(result.retro_gates),
        format_setup_by_strategy(result.setup_by_strategy),
    ]
    return "\n\n".join(sections) + "\n"


def replays_to_dict(result: ReplaysResult) -> dict:
    cov = result.retro_gates.coverage
    return {
        "fingerprint_prefix": result.fingerprint_prefix,
        "n": result.n,
        "fee_sensitivity": {
            "fee_rates": list(result.fee_sensitivity.fee_rates),
            "break_even_fee_rate": result.fee_sensitivity.break_even_fee_rate,
            "rows": [
                {
                    "label": row.label,
                    "group": row.group,
                    "n": row.n,
                    "gross": row.gross,
                    "actual_fees": row.actual_fees,
                    "actual_net": row.actual_net,
                    "fees_at_rate": list(row.fees_at_rate),
                    "net_at_rate": list(row.net_at_rate),
                }
                for row in result.fee_sensitivity.rows
            ],
        },
        "retro_gates": {
            "fee_hurdle_r_max": result.retro_gates.fee_hurdle_r_max,
            "coverage": {
                "stored": cov.stored,
                "recomputed": cov.recomputed,
                "unavailable": cov.unavailable,
                "n": cov.n,
            },
            "rows": [
                {
                    "name": row.name,
                    "blocked_n": row.blocked_n,
                    "blocked_net": row.blocked_net,
                    "blocked_mean_r": row.blocked_mean_r,
                    "kept_n": row.kept_n,
                    "kept_net": row.kept_net,
                    "kept_mean_r": row.kept_mean_r,
                    "total": row.total,
                }
                for row in result.retro_gates.rows
            ],
        },
        "setup_by_strategy": {
            "rows": [
                {
                    "setup": row.setup,
                    "strategy": row.strategy,
                    "n": row.n,
                    "gross": row.gross,
                    "fees": row.fees,
                    "net": row.net,
                    "mean_net_r": row.mean_net_r,
                    "net_win_pct": row.net_win_pct,
                    "small_n": row.small_n,
                }
                for row in result.setup_by_strategy.rows
            ],
        },
    }


def format_replays_json(result: ReplaysResult) -> str:
    return json.dumps(replays_to_dict(result), indent=2) + "\n"
