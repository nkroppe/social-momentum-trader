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

from ..config import MarketConfig, SignalsConfig, StrategyConfig, get_market, get_signals
from ..logging_setup import get_logger
from ..market import horizon_volatility
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
    def __init__(
        self,
        store: Store,
        signals: SignalsConfig | None = None,
        market_cfg: MarketConfig | None = None,
    ):
        self.store = store
        self.signals = signals if signals is not None else get_signals()
        self.market_cfg = market_cfg if market_cfg is not None else get_market()

    def size_position(
        self, candidate: TradeCandidate, strategy: StrategyConfig, equity_alloc: float
    ) -> tuple[float, str]:
        """Notional for this entry, reduced for tier and asset volatility.

        Both adjustments are multiplicative and capped at 1.0, so sizing can
        only ever come in under the hard max_position_pct limit. Without this a
        fixed 10% notional puts many times more risk into a sub-cent token than
        into BTC.
        """
        base = equity_alloc * strategy.max_position_pct
        notes: list[str] = []

        tier = self.signals.tier(candidate.tier)
        tier_mult = max(0.0, min(tier.max_position_pct_mult, 1.0))
        if tier_mult != 1.0:
            notes.append(f"tier[{candidate.tier}]x{tier_mult:.2f}")

        sizing = self.market_cfg.sizing
        vol_mult = 1.0
        if sizing.enabled and candidate.atr_pct > 0:
            # Compare volatility over the same holding period the exits use, so
            # both scale together rather than against different horizons.
            hvol = horizon_volatility(
                candidate.atr_pct,
                strategy.time_stop_hours,
                self.market_cfg.candle_granularity_seconds,
            )
            vol_mult = sizing.target_atr_pct / hvol
            vol_mult = max(sizing.min_scale, min(vol_mult, sizing.max_scale))
            notes.append(f"vol[{hvol:.2%}/{strategy.time_stop_hours}h]x{vol_mult:.2f}")

        notional = round(base * tier_mult * vol_mult, 2)
        return notional, " ".join(notes) if notes else "unscaled"

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

        notional, sizing_note = self.size_position(candidate, st, equity_alloc)
        if notional < st.min_order_notional_usd:
            return RiskDecision(
                False,
                0.0,
                f"position notional ${notional:.2f} < min ${st.min_order_notional_usd:.2f} "
                f"({sizing_note})",
            )

        return RiskDecision(True, notional, f"approved ({sizing_note})")
