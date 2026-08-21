"""Pure after-cost performance metrics shared by replay and operational reports."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PerformanceTrade:
    """One completed round trip expressed before and after execution costs."""

    gross_pnl: float
    fees: float
    modeled_slippage: float
    entry_notional: float
    exit_notional: float
    initial_risk: float = 0.0
    net_pnl: float | None = None

    @property
    def reconciled_net_pnl(self) -> float:
        calculated = self.gross_pnl - self.fees - self.modeled_slippage
        if self.net_pnl is not None and not math.isclose(
            self.net_pnl, calculated, rel_tol=1e-9, abs_tol=1e-8
        ):
            raise ValueError(
                "trade after-cost reconciliation failed: "
                f"net={self.net_pnl:.12f}, gross-fees-slippage={calculated:.12f}"
            )
        return calculated


@dataclass(frozen=True)
class EquityPoint:
    timestamp: datetime | int
    equity: float
    gross_exposure: float = 0.0


def calculate_performance(
    trades: Iterable[PerformanceTrade],
    equity_curve: Iterable[EquityPoint],
    initial_equity: float,
) -> dict[str, float | int | None]:
    """Calculate reconciled, after-cost strategy statistics.

    Turnover is total bought and sold notional divided by initial equity.
    Average exposure is the time-weighted gross-exposure/equity ratio.
    """
    if initial_equity <= 0:
        raise ValueError("initial_equity must be positive")
    rows = list(trades)
    curve = list(equity_curve)
    net_values = [row.reconciled_net_pnl for row in rows]
    net_pnl = sum(net_values)
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    fees = sum(row.fees for row in rows)
    slippage = sum(row.modeled_slippage for row in rows)
    net_rs = [
        value / row.initial_risk
        for row, value in zip(rows, net_values, strict=True)
        if row.initial_risk > 0
    ]

    peak = initial_equity
    max_drawdown = 0.0
    for point in curve:
        peak = max(peak, point.equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, (peak - point.equity) / peak)

    exposure_area = 0.0
    duration = 0.0
    if len(curve) == 1:
        exposure_area = curve[0].gross_exposure / curve[0].equity if curve[0].equity > 0 else 0.0
        duration = 1.0
    for left, right in zip(curve, curve[1:], strict=False):
        left_ts = (
            left.timestamp.timestamp()
            if isinstance(left.timestamp, datetime)
            else float(left.timestamp)
        )
        right_ts = (
            right.timestamp.timestamp()
            if isinstance(right.timestamp, datetime)
            else float(right.timestamp)
        )
        elapsed = max(right_ts - left_ts, 0.0)
        ratio = left.gross_exposure / left.equity if left.equity > 0 else 0.0
        exposure_area += ratio * elapsed
        duration += elapsed

    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(rows),
        "net_pnl": net_pnl,
        "net_return": net_pnl / initial_equity,
        "expectancy": net_pnl / len(rows) if rows else 0.0,
        "net_r_expectancy": sum(net_rs) / len(net_rs) if net_rs else 0.0,
        "profit_factor": (
            gross_profit / gross_loss if gross_loss > 0 else (None if gross_profit > 0 else 0.0)
        ),
        "max_marked_equity_drawdown": max_drawdown,
        "turnover": (sum(row.entry_notional + row.exit_notional for row in rows) / initial_equity),
        "average_exposure": exposure_area / duration if duration > 0 else 0.0,
        "fees": fees,
        "modeled_slippage": slippage,
        "win_rate": len(wins) / len(rows) if rows else 0.0,
    }
