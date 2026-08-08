"""Trade manager: opens approved entries and manages exits (TP/SL/time-stop/kill).

Exit levels and the time-stop are derived from the opening strategy's params
and stored on the trade, so each position is managed by its own methodology.
Capital is tracked per strategy via independent allocation equity.
"""

from __future__ import annotations

from datetime import timedelta

from ..config import (
    MarketConfig,
    Settings,
    StrategyConfig,
    TradeAlertsConfig,
    UniverseConfig,
    get_market,
)
from ..logging_setup import get_logger
from ..market import MarketData, atr, horizon_volatility
from ..models import ExitReason, Trade, TradeStatus, utcnow
from ..ops.alerts import Alerter
from ..ops.reports import trade_closed_alert, trade_opened_alert
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
        market: MarketData | None = None,
        market_cfg: MarketConfig | None = None,
        alerter: Alerter | None = None,
        trade_alerts: TradeAlertsConfig | None = None,
    ):
        self.settings = settings
        self.universe = universe
        self.store = store
        self.broker = broker
        self.market = market
        self.market_cfg = market_cfg if market_cfg is not None else get_market()
        self.alerter = alerter
        self.trade_alerts = trade_alerts if trade_alerts is not None else TradeAlertsConfig()

    # ---- Notifications -------------------------------------------------------

    def _notify(self, subject: str, body: str) -> None:
        """Best-effort: a failed notification must never abort a trade."""
        if self.alerter is None or not self.trade_alerts.enabled:
            return
        try:
            self.alerter.notify(subject, body)
        except Exception as exc:  # noqa: BLE001
            log.warning("trade notification failed: %s", exc)

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

    def _atr_pct(self, product_id: str, price: float) -> float:
        if self.market is None or price <= 0:
            return 0.0
        candles = self.market.candles(product_id)
        if len(candles) < 2:
            return 0.0
        return atr(candles, self.market_cfg.atr_periods) / price

    def horizon_volatility(self, atr_pct: float, strategy: StrategyConfig) -> float:
        return horizon_volatility(
            atr_pct, strategy.time_stop_hours, self.market_cfg.candle_granularity_seconds
        )

    def exit_levels(
        self, entry_price: float, candidate: TradeCandidate, strategy: StrategyConfig
    ) -> tuple[float, float, str]:
        """Take-profit and stop-loss prices for this entry.

        Under ATR sizing the targets track each asset's own volatility over this
        strategy's holding period. A single fixed percentage cannot fit a
        universe spanning BTC and a sub-cent token: it makes BTC exits
        unreachable (so nearly every trade ends on the time stop at a random
        price) while sitting inside the noise band of the micro cap (so it stops
        out on nothing).
        """
        tp_pct = strategy.take_profit_pct
        sl_pct = strategy.stop_loss_pct
        note = "fixed"

        if strategy.exit_style == "atr":
            atr_pct = candidate.atr_pct or self._atr_pct(candidate.product_id, entry_price)
            if atr_pct > 0:
                horizon_vol = self.horizon_volatility(atr_pct, strategy)
                sl_pct = horizon_vol * strategy.atr_stop_loss_mult
                sl_pct = max(strategy.atr_min_stop_pct, min(sl_pct, strategy.atr_max_stop_pct))
                # Derive TP from the clamped stop so the reward:risk ratio holds.
                rr = strategy.atr_take_profit_mult / max(strategy.atr_stop_loss_mult, 1e-9)
                tp_pct = sl_pct * rr
                note = f"atr={atr_pct:.2%}/bar horizon={horizon_vol:.2%}"
            else:
                note = "fixed (no ATR history)"

        # A target inside round-trip costs is not a trade worth taking.
        min_tp = 3.0 * strategy.assumed_fee_pct_per_side
        if tp_pct < min_tp:
            tp_pct = min_tp
            note += " tp raised to fee floor"

        tp = round(entry_price * (1 + tp_pct), 8)
        sl = round(entry_price * (1 - sl_pct), 8)
        return tp, sl, f"{note} tp={tp_pct:.2%} sl={sl_pct:.2%}"

    def open_position(
        self, candidate: TradeCandidate, notional_usd: float, strategy: StrategyConfig
    ) -> Trade:
        entry_price = self.broker.current_price(candidate.product_id)
        tp, sl, exit_note = self.exit_levels(entry_price, candidate, strategy)

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
            "OPENED[%s] %s qty=%.8f entry=%.6f tp=%.6f sl=%.6f notional=$%.2f (%s)",
            strategy.name,
            trade.ticker,
            trade.qty,
            trade.entry_price,
            tp,
            sl,
            notional_usd,
            exit_note,
        )
        if self.trade_alerts.on_open:
            self._notify(*trade_opened_alert(trade, notional_usd, exit_note))
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
        if self.trade_alerts.on_close:
            self._notify(*trade_closed_alert(trade))

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
