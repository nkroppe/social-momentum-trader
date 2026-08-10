"""Hard risk gate.

Every proposed entry passes through here. These checks are code-enforced and
cannot be overridden by the signal engine or any model. Fail-closed: any error
or ambiguity results in rejection.

The gate is strategy-aware: all limits and PnL are scoped to the candidate's
strategy, so one strategy hitting a limit or loss-halt never blocks the other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import MarketConfig, SignalsConfig, StrategyConfig, get_market, get_signals
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
    risk_budget_usd: float = 0.0


class RiskGate:
    def __init__(
        self,
        store: Store,
        signals: SignalsConfig | None = None,
        market_cfg: MarketConfig | None = None,
        mark_price: Callable[[str], float | None] | None = None,
    ):
        self.store = store
        self.signals = signals if signals is not None else get_signals()
        self.market_cfg = market_cfg if market_cfg is not None else get_market()
        self.mark_price = mark_price

    def size_position(
        self, candidate: TradeCandidate, strategy: StrategyConfig, equity_alloc: float
    ) -> tuple[float, str]:
        """Risk-budget notional capped by every exposure constraint."""
        hard_cap = equity_alloc * strategy.max_position_pct
        stop_pct = candidate.stop_pct or strategy.stop_loss_pct
        if stop_pct <= 0:
            return 0.0, "invalid stop distance"
        risk_budget = equity_alloc * strategy.risk_per_trade_pct
        risk_sized = risk_budget / stop_pct
        notes: list[str] = []

        tier = self.signals.tier(candidate.tier)
        tier_mult = max(0.0, min(tier.max_position_pct_mult, 1.0))
        if tier_mult != 1.0:
            notes.append(f"tier[{candidate.tier}]x{tier_mult:.2f}")

        sizing = self.market_cfg.sizing
        vol_mult = 1.0
        if sizing.enabled and candidate.atr_pct > 0:
            # Compare like-for-like trigger-bar ATR. Stop distance already
            # carries setup-specific horizon risk; annualizing again here would
            # collapse both calm and volatile 15m assets into the same floor.
            vol_mult = sizing.target_atr_pct / candidate.atr_pct
            vol_mult = max(sizing.min_scale, min(vol_mult, sizing.max_scale))
            notes.append(f"vol[{candidate.atr_pct:.2%}/bar]x{vol_mult:.2f}")

        conviction_mult = max(0.0, min(candidate.size_multiplier, candidate.conviction, 1.0))
        if conviction_mult != 1.0:
            notes.append(f"convictionx{conviction_mult:.2f}")

        adjusted_cap = hard_cap * tier_mult * vol_mult * conviction_mult
        notional = round(min(risk_sized, adjusted_cap, hard_cap), 2)
        notes.insert(0, f"risk={strategy.risk_per_trade_pct:.2%}/stop={stop_pct:.2%}")
        return notional, " ".join(notes)

    def _open_pnl(
        self, strategy: str, opened_since: datetime | None = None
    ) -> tuple[float | None, str]:
        trades = self.store.open_trades(strategy)
        if not trades:
            return 0.0, ""
        if self.mark_price is None:
            return None, "mark-price callback unavailable"
        pnl = 0.0
        for trade in trades:
            opened = (
                trade.opened_at
                if trade.opened_at.tzinfo
                else trade.opened_at.replace(tzinfo=utcnow().tzinfo)
            )
            if opened_since is not None and opened < opened_since:
                continue
            try:
                mark = self.mark_price(trade.product_id)
            except Exception as exc:  # noqa: BLE001
                return None, f"mark quote failed for {trade.product_id}: {exc}"
            if mark is None or mark <= 0:
                return None, f"invalid mark quote for {trade.product_id}"
            pnl += (mark - trade.entry_price) * trade.qty
            pnl += trade.partial_realized_pnl or 0.0
        return pnl, ""

    def portfolio_halted(
        self, strategy: StrategyConfig, start_equity: float
    ) -> tuple[bool, str]:
        """UTC day/week equity-drawdown halts, scoped to one strategy."""
        open_pnl, quote_error = self._open_pnl(strategy.name)
        if open_pnl is None:
            return True, f"loss halt conservative: {quote_error}"

        current_equity = (
            start_equity + self.store.total_realized_pnl(strategy.name) + open_pnl
        )
        now = utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        day_open_pnl, day_quote_error = self._open_pnl(strategy.name, day_start)
        week_open_pnl, week_quote_error = self._open_pnl(strategy.name, week_start)
        if day_open_pnl is None or week_open_pnl is None:
            return True, f"loss halt conservative: {day_quote_error or week_quote_error}"
        day_initial = (
            current_equity
            - self.store.realized_pnl_since(day_start, strategy.name)
            - day_open_pnl
        )
        week_initial = (
            current_equity
            - self.store.realized_pnl_since(week_start, strategy.name)
            - week_open_pnl
        )
        day_baseline = self.store.risk_equity_baseline(
            strategy.name, "day", day_start, day_initial
        )
        week_baseline = self.store.risk_equity_baseline(
            strategy.name, "week", week_start, week_initial
        )
        daily_pnl = current_equity - day_baseline
        weekly_pnl = current_equity - week_baseline

        if day_baseline > 0 and daily_pnl / day_baseline <= strategy.daily_loss_halt_pct:
            return True, f"daily loss halt: {daily_pnl:.2f} ({daily_pnl / day_baseline:.1%})"
        if week_baseline > 0 and weekly_pnl / week_baseline <= strategy.weekly_loss_halt_pct:
            return True, f"weekly loss halt: {weekly_pnl:.2f} ({weekly_pnl / week_baseline:.1%})"
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

        if candidate.entry_price > 0 and candidate.structure_stop > 0:
            if self.mark_price is None:
                return RiskDecision(False, 0.0, "fresh entry quote unavailable")
            try:
                quote = self.mark_price(candidate.product_id)
            except Exception as exc:  # noqa: BLE001
                return RiskDecision(False, 0.0, f"fresh entry quote failed: {exc}")
            if quote is None or quote <= candidate.structure_stop:
                return RiskDecision(False, 0.0, "fresh quote is at/below structure stop")
            slippage = abs(quote - candidate.entry_price) / candidate.entry_price
            if slippage > st.entry.max_entry_slippage_pct:
                return RiskDecision(
                    False,
                    0.0,
                    f"setup stale: quote moved {slippage:.2%} "
                    f"(max {st.entry.max_entry_slippage_pct:.2%})",
                )
            candidate.entry_price = quote
            candidate.stop_pct = (quote - candidate.structure_stop) / quote

        notional, sizing_note = self.size_position(candidate, st, equity_alloc)
        if notional < st.min_order_notional_usd:
            return RiskDecision(
                False,
                0.0,
                f"position notional ${notional:.2f} < min ${st.min_order_notional_usd:.2f} "
                f"({sizing_note})",
            )

        return RiskDecision(
            True,
            notional,
            f"approved ({sizing_note})",
            risk_budget_usd=equity_alloc * st.risk_per_trade_pct,
        )
