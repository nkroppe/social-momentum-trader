"""Hard risk gate.

Every proposed entry passes through here. These checks are code-enforced and
cannot be overridden by the signal engine or any model. Fail-closed: any error
or ambiguity results in rejection.

The gate is strategy-aware: all limits and PnL are scoped to the candidate's
strategy, so one strategy hitting a limit or loss-halt never blocks the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from ..config import StrategyConfig
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
    def __init__(self, store: Store):
        self.store = store

    def portfolio_halted(
        self, strategy: StrategyConfig, start_equity: float
    ) -> tuple[bool, str]:
        """Daily / weekly loss halts, scoped to this strategy's allocation."""
        day_ago = utcnow() - timedelta(days=1)
        week_ago = utcnow() - timedelta(days=7)
        daily_pnl = self.store.realized_pnl_since(day_ago, strategy.name)
        weekly_pnl = self.store.realized_pnl_since(week_ago, strategy.name)

        if start_equity > 0:
            if daily_pnl / start_equity <= strategy.daily_loss_halt_pct:
                return True, f"daily loss halt: {daily_pnl:.2f} ({daily_pnl / start_equity:.1%})"
            if weekly_pnl / start_equity <= strategy.weekly_loss_halt_pct:
                return True, f"weekly loss halt: {weekly_pnl:.2f} ({weekly_pnl / start_equity:.1%})"
        return False, ""

    def evaluate(
        self,
        candidate: TradeCandidate,
        strategy: StrategyConfig,
        equity_alloc: float,
        start_equity_alloc: float,
    ) -> RiskDecision:
        """Evaluate a candidate against `strategy`'s own limits and allocation.

        `equity_alloc` is the strategy's current allocation equity (its half),
        `start_equity_alloc` is its starting allocation (for loss-halt math).
        """
        st = strategy

        halted, why = self.portfolio_halted(st, start_equity_alloc)
        if halted:
            return RiskDecision(False, 0.0, why)

        # One open position per ticker PER STRATEGY (a ticker may be held by both).
        if self.store.open_trade_for(candidate.ticker, st.name) is not None:
            return RiskDecision(False, 0.0, "already have an open position for this ticker")

        if self.store.count_open_trades(st.name) >= st.max_open_positions:
            return RiskDecision(False, 0.0, f"max_open_positions={st.max_open_positions} reached")

        day_ago = utcnow() - timedelta(days=1)
        if self.store.count_trades_since(day_ago, st.name) >= st.max_trades_per_day:
            return RiskDecision(False, 0.0, f"max_trades_per_day={st.max_trades_per_day} reached")

        last_stop = self.store.last_stop_out_for(candidate.ticker, st.name)
        if last_stop is not None:
            last_stop_utc = (
                last_stop if last_stop.tzinfo else last_stop.replace(tzinfo=utcnow().tzinfo)
            )
            if utcnow() - last_stop_utc < timedelta(minutes=st.cooldown_minutes_after_stop):
                return RiskDecision(False, 0.0, "within cooldown after recent stop-out")

        notional = round(equity_alloc * st.max_position_pct, 2)
        if notional < st.min_order_notional_usd:
            return RiskDecision(
                False,
                0.0,
                f"position notional ${notional:.2f} < min ${st.min_order_notional_usd:.2f}",
            )

        return RiskDecision(True, notional, "approved")
