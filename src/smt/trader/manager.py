"""Trade manager: opens approved entries and manages exits (TP/SL/time-stop/kill).

Exit levels and the time-stop are derived from the opening strategy's params
and stored on the trade, so each position is managed by its own methodology.
Capital is tracked per strategy via independent allocation equity.
"""

from __future__ import annotations

from datetime import timedelta

from ..config import Settings, StrategyConfig, UniverseConfig
from ..logging_setup import get_logger
from ..models import ExitReason, Trade, TradeStatus, utcnow
from ..store import Store
from .broker import Broker
from .signals import TradeCandidate

log = get_logger("smt.manager")


class TradeManager:
    def __init__(
        self,
        settings: Settings,
        universe: UniverseConfig,
        store: Store,
        broker: Broker,
    ):
        self.settings = settings
        self.universe = universe
        self.store = store
        self.broker = broker

    # ---- Equity ------------------------------------------------------------

    def equity(self) -> float:
        """Total portfolio equity across all strategies.

        Paper: start equity + all realized PnL + mark-to-market of open trades.
        Live: read from the exchange (isolated portfolio).
        """
        if self.broker.name == "coinbase":
            try:
                return self.broker.portfolio_equity_usd()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to read live equity, falling back: %s", exc)

        eq = self.settings.paper_start_equity + self.store.total_realized_pnl()
        for t in self.store.open_trades():
            price = self.broker.current_price(t.product_id)
            eq += (price - t.entry_price) * t.qty
        return eq

    def allocation_start_equity(self, strategy: StrategyConfig) -> float:
        """The strategy's starting capital slice."""
        return self.settings.paper_start_equity * strategy.allocation

    def allocation_equity(self, strategy: StrategyConfig) -> float:
        """Current equity of this strategy's independent allocation.

        = its starting slice + its own realized PnL + MTM of its open trades.
        This keeps the two strategies' capital fully independent.
        """
        if self.broker.name == "coinbase":
            try:
                return self.broker.portfolio_equity_usd() * strategy.allocation  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001
                log.warning("failed to read live equity, falling back: %s", exc)

        eq = self.allocation_start_equity(strategy) + self.store.total_realized_pnl(strategy.name)
        for t in self.store.open_trades(strategy.name):
            price = self.broker.current_price(t.product_id)
            eq += (price - t.entry_price) * t.qty
        return eq

    # ---- Entry -------------------------------------------------------------

    def open_position(
        self, candidate: TradeCandidate, notional_usd: float, strategy: StrategyConfig
    ) -> Trade:
        entry_price = self.broker.current_price(candidate.product_id)
        tp = round(entry_price * (1 + strategy.take_profit_pct), 8)
        sl = round(entry_price * (1 - strategy.stop_loss_pct), 8)

        fill = self.broker.open_long(candidate.product_id, notional_usd, tp, sl)
        trade = Trade(
            ticker=candidate.ticker,
            strategy=strategy.name,
            product_id=candidate.product_id,
            is_live=(self.broker.name == "coinbase"),
            status=TradeStatus.OPEN,
            qty=fill.qty,
            entry_price=fill.price,
            entry_notional=notional_usd,
            take_profit=tp,
            stop_loss=sl,
            time_stop_at=utcnow() + timedelta(hours=strategy.time_stop_hours),
            fees_paid=fill.fee,
            broker_entry_order_id=fill.order_id,
        )
        trade = self.store.add_trade(trade)
        log.info(
            "OPENED[%s] %s qty=%.8f entry=%.6f tp=%.6f sl=%.6f notional=$%.2f",
            strategy.name,
            trade.ticker,
            trade.qty,
            trade.entry_price,
            tp,
            sl,
            notional_usd,
        )
        return trade

    # ---- Exit --------------------------------------------------------------

    def _close(self, trade: Trade, price: float, reason: ExitReason) -> None:
        # For paper we simulate the sell; for live-with-server-brackets TP/SL are
        # already handled by the exchange, so we only actively close on time-stop/kill.
        fill = self.broker.close_long(trade.product_id, trade.qty)
        exit_price = (
            price if reason in (ExitReason.TAKE_PROFIT, ExitReason.STOP_LOSS) else fill.price
        )
        gross = (exit_price - trade.entry_price) * trade.qty
        total_fees = trade.fees_paid + fill.fee
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.fees_paid = total_fees
        trade.realized_pnl = gross - total_fees
        trade.status = TradeStatus.CLOSED
        trade.closed_at = utcnow()
        self.store.update_trade(trade)
        log.info(
            "CLOSED[%s] %s reason=%s exit=%.6f pnl=$%.2f (fees=$%.2f)",
            trade.strategy,
            trade.ticker,
            reason.value,
            exit_price,
            trade.realized_pnl,
            total_fees,
        )

    def manage_open_trades(self, force_flatten: bool = False) -> None:
        # Kill switch flattens EVERY strategy's positions.
        for trade in self.store.open_trades():
            # Normalize tz for time-stop comparison.
            tstop = trade.time_stop_at
            tstop = tstop if tstop.tzinfo else tstop.replace(tzinfo=utcnow().tzinfo)

            if force_flatten:
                self._close(
                    trade, self.broker.current_price(trade.product_id), ExitReason.KILL_SWITCH
                )
                continue

            price = self.broker.current_price(trade.product_id)

            # TP/SL: simulated in paper; server-side in live (skip to avoid double close).
            if not self.broker.server_side_brackets:
                if price >= trade.take_profit:
                    self._close(trade, trade.take_profit, ExitReason.TAKE_PROFIT)
                    continue
                if price <= trade.stop_loss:
                    self._close(trade, trade.stop_loss, ExitReason.STOP_LOSS)
                    continue

            if utcnow() >= tstop:
                self._close(trade, price, ExitReason.TIME_STOP)
