"""Hard risk gate.

Every proposed entry passes through here. These checks are code-enforced and
cannot be overridden by the signal engine or any model. Fail-closed: any error
or ambiguity results in rejection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..config import RiskConfig
from ..logging_setup import get_logger
from ..models import utcnow
from ..store import Store
from .signals import TradeCandidate

log = get_logger("smt.risk")


@dataclass
class RiskDecision:
    approved: bool
    notional_usd: float
    reason: str


class RiskGate:
    def __init__(self, risk: RiskConfig, store: Store):
        self.risk = risk
        self.store = store

    def portfolio_halted(self, equity: float, start_equity: float) -> tuple[bool, str]:
        """Daily / weekly loss halts."""
        day_ago = utcnow() - timedelta(days=1)
        week_ago = utcnow() - timedelta(days=7)
        daily_pnl = self.store.realized_pnl_since(day_ago)
        weekly_pnl = self.store.realized_pnl_since(week_ago)

        if start_equity > 0:
            if daily_pnl / start_equity <= self.risk.daily_loss_halt_pct:
                return True, f"daily loss halt: {daily_pnl:.2f} ({daily_pnl / start_equity:.1%})"
            if weekly_pnl / start_equity <= self.risk.weekly_loss_halt_pct:
                return True, f"weekly loss halt: {weekly_pnl:.2f} ({weekly_pnl / start_equity:.1%})"
        return False, ""

    def evaluate(
        self, candidate: TradeCandidate, equity: float, start_equity: float
    ) -> RiskDecision:
        r = self.risk

        halted, why = self.portfolio_halted(equity, start_equity)
        if halted:
            return RiskDecision(False, 0.0, why)

        if self.store.open_trade_for(candidate.ticker) is not None:
            return RiskDecision(False, 0.0, "already have an open position for this ticker")

        if self.store.count_open_trades() >= r.max_open_positions:
            return RiskDecision(False, 0.0, f"max_open_positions={r.max_open_positions} reached")

        day_ago = utcnow() - timedelta(days=1)
        if self.store.count_trades_since(day_ago) >= r.max_trades_per_day:
            return RiskDecision(False, 0.0, f"max_trades_per_day={r.max_trades_per_day} reached")

        last_stop = self.store.last_stop_out_for(candidate.ticker)
        if last_stop is not None:
            # Normalize tz for comparison.
            last_stop_utc = (
                last_stop if last_stop.tzinfo else last_stop.replace(tzinfo=utcnow().tzinfo)
            )
            if utcnow() - last_stop_utc < timedelta(minutes=r.cooldown_minutes_after_stop):
                return RiskDecision(False, 0.0, "within cooldown after recent stop-out")

        notional = round(equity * r.max_position_pct, 2)
        if notional < r.min_order_notional_usd:
            return RiskDecision(
                False,
                0.0,
                f"position notional ${notional:.2f} < min ${r.min_order_notional_usd:.2f}",
            )

        return RiskDecision(True, notional, "approved")
