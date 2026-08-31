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
from ..ops.reports import trade_closed_alert, trade_opened_alert, trade_partial_alert
from ..store import Store, stable_config_fingerprint
from .broker import Broker
from .execution import ExecutionCostEstimator, conservative_quote
from .exit_policy import (
    ExitActionKind,
    bar_step,
    chandelier_ratchet,
    first_partial_economics,
    first_partial_quantity,
    initial_levels,
    legacy_profile,
    quote_step,
    resolve_profile,
    time_exit_reason,
)
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
        strategies: list[StrategyConfig] | None = None,
        config_fingerprint: str | None = None,
    ):
        self.settings = settings
        self.universe = universe
        self.store = store
        self.broker = broker
        self.market = market
        self.market_cfg = market_cfg if market_cfg is not None else get_market()
        self.alerter = alerter
        self.trade_alerts = trade_alerts if trade_alerts is not None else TradeAlertsConfig()
        self.strategies = {strategy.name: strategy for strategy in (strategies or [])}
        self.config_fingerprint = config_fingerprint or stable_config_fingerprint()

    # ---- Notifications -------------------------------------------------------

    def _notify(self, subject: str, body: str) -> None:
        """Best-effort: a failed notification must never abort a trade."""
        if self.alerter is None or not self.trade_alerts.enabled:
            return
        try:
            delivered = self.alerter.notify(subject, body)
        except Exception as exc:  # noqa: BLE001
            log.warning("trade notification failed: %s", exc)
            return
        if not delivered:
            log.warning("trade notification not delivered: %s", subject)

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
            eq += t.partial_realized_pnl or 0.0
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
            eq += t.partial_realized_pnl or 0.0
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
        atr_pct = candidate.atr_pct or self._atr_pct(candidate.product_id, entry_price)
        levels = initial_levels(
            entry_price,
            candidate.structure_stop,
            strategy.exit_profile,
            atr_pct=atr_pct,
            candle_granularity_seconds=self.market_cfg.candle_granularity_seconds,
            assumed_fee_pct_per_side=strategy.assumed_fee_pct_per_side,
        )
        return levels.take_profit, levels.stop_loss, levels.note

    def open_position(
        self,
        candidate: TradeCandidate,
        notional_usd: float,
        strategy: StrategyConfig,
        risk_budget_usd: float = 0.0,
    ) -> Trade:
        profile = resolve_profile(strategy.exit_profile)
        exit_snapshot = profile.snapshot()
        entry_price = self.broker.current_price(candidate.product_id)
        tp, sl, exit_note = self.exit_levels(entry_price, candidate, strategy)

        fill = self.broker.open_long(candidate.product_id, notional_usd, tp, sl)
        paper_bar_cursor = 0
        cursor_reader = getattr(self.broker, "last_closed_bar_ts", None)
        if (
            self.broker.name == "paper"
            and not getattr(self.broker, "offline_simulation", True)
            and callable(cursor_reader)
        ):
            paper_bar_cursor = int(cursor_reader(candidate.product_id))
        if 0 < candidate.structure_stop < fill.price:
            sl = round(candidate.structure_stop, 8)
            tp = round(
                fill.price + (fill.price - sl) * strategy.partial_take_profit_r,
                8,
            )
        else:
            tp, sl, exit_note = self.exit_levels(fill.price, candidate, strategy)
        initial_risk = max(fill.price - sl, 0.0)
        actual_risk = initial_risk * fill.qty
        fill_slippage = abs(fill.price - entry_price) / entry_price if entry_price > 0 else 0.0
        entry_risk_breach = (
            risk_budget_usd > 0
            and candidate.entry_price > 0
            and candidate.structure_stop > 0
            and (
                actual_risk > risk_budget_usd * 1.02
                or fill_slippage > strategy.entry.max_entry_slippage_pct
            )
        )

        if entry_risk_breach and self.broker.name == "paper":
            unwind = self.broker.close_long(candidate.product_id, fill.qty)
            fees = fill.fee + unwind.fee
            trade = Trade(
                ticker=candidate.ticker,
                strategy=strategy.name,
                product_id=candidate.product_id,
                is_live=False,
                status=TradeStatus.CLOSED,
                qty=fill.qty,
                original_qty=fill.qty,
                entry_price=fill.price,
                entry_notional=notional_usd,
                take_profit=tp,
                stop_loss=sl,
                highest_price=fill.price,
                initial_risk_per_unit=initial_risk,
                entry_fee_paid=fill.fee,
                setup=candidate.setup,
                last_processed_paper_bar_ts=paper_bar_cursor,
                config_fingerprint=self.config_fingerprint,
                exit_profile_label=profile.label,
                exit_snapshot=exit_snapshot,
                time_stop_at=utcnow(),
                exit_price=unwind.price,
                exit_reason=ExitReason.ENTRY_RISK,
                realized_pnl=(unwind.price - fill.price) * fill.qty - fees,
                fees_paid=fees,
                broker_entry_order_id=fill.order_id,
                closed_at=utcnow(),
            )
            trade = self.store.add_trade(trade)
            log.error(
                "UNWOUND[%s] %s entry risk $%.2f > budget $%.2f or slippage %.2f%%",
                strategy.name,
                trade.ticker,
                actual_risk,
                risk_budget_usd,
                fill_slippage * 100,
            )
            if self.trade_alerts.on_close:
                self._notify(*trade_closed_alert(trade))
            return trade

        trade = Trade(
            ticker=candidate.ticker,
            strategy=strategy.name,
            product_id=candidate.product_id,
            is_live=(self.broker.name == "coinbase"),
            status=TradeStatus.OPEN,
            qty=fill.qty,
            original_qty=fill.qty,
            entry_price=fill.price,
            entry_notional=notional_usd,
            take_profit=tp,
            stop_loss=sl,
            highest_price=fill.price,
            initial_risk_per_unit=initial_risk,
            partial_taken=False,
            partial_realized_pnl=0.0,
            trailing_stop=0.0,
            entry_fee_paid=fill.fee,
            setup=candidate.setup,
            last_processed_paper_bar_ts=paper_bar_cursor,
            config_fingerprint=self.config_fingerprint,
            exit_profile_label=profile.label,
            exit_snapshot=exit_snapshot,
            time_stop_at=utcnow() + timedelta(hours=profile.time_stop_hours),
            fees_paid=fill.fee,
            broker_entry_order_id=fill.order_id,
        )
        self.strategies[strategy.name] = strategy
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

    def _close(
        self,
        trade: Trade,
        price: float,
        reason: ExitReason,
        *,
        emergency: bool = False,
    ) -> None:
        # For paper we simulate the sell; for live-with-server-brackets TP/SL are
        # already handled by the exchange, so we only actively close on time-stop/kill.
        fill = self.broker.close_long(
            trade.product_id,
            trade.qty,
            reference_price=price,
            emergency=emergency,
        )
        exit_price = fill.price
        gross = (exit_price - trade.entry_price) * trade.qty
        total_fees = trade.fees_paid + fill.fee
        original_qty = trade.original_qty or trade.qty
        remaining_entry_fee = trade.entry_fee_paid * (trade.qty / original_qty)
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.fees_paid = total_fees
        trade.realized_pnl = trade.partial_realized_pnl + gross - fill.fee - remaining_entry_fee
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

    def _kill_flatten(self, trade: Trade) -> None:
        """Flatten for the kill switch, failing closed even when quotes are down."""
        try:
            price = self.broker.current_price(trade.product_id)
            self._close(trade, price, ExitReason.KILL_SWITCH)
            return
        except Exception as exc:  # noqa: BLE001 - mandated flatten must continue
            log.warning(
                "Kill flatten primary path failed for %s: %s",
                trade.product_id,
                exc,
            )
        try:
            self._close(
                trade,
                trade.entry_price,
                ExitReason.KILL_SWITCH,
                emergency=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "Kill flatten emergency path failed for %s: %s",
                trade.product_id,
                exc,
            )

    def _strategy_for(self, trade: Trade) -> StrategyConfig | None:
        return self.strategies.get(trade.strategy)

    @staticmethod
    def _profile_for(trade: Trade):
        if trade.exit_snapshot:
            return resolve_profile(trade.exit_snapshot)
        return legacy_profile(trade.strategy)

    def _trail_atr(self, trade: Trade, profile) -> float:
        atr_abs = 0.0
        if self.market is not None:
            candles = self.market.candles(trade.product_id, profile.trail_granularity_seconds)
            atr_abs = atr(candles, self.market_cfg.atr_periods)
        if atr_abs <= 0:
            atr_abs = trade.initial_risk_per_unit
        return atr_abs

    def _chandelier_stop(self, trade: Trade, profile) -> float:
        atr_abs = self._trail_atr(trade, profile)
        if atr_abs <= 0:
            return trade.trailing_stop
        return chandelier_ratchet(
            trade.trailing_stop,
            trade.stop_loss,
            trade.highest_price,
            atr_abs,
            profile.chandelier_atr_mult,
        )

    def _take_partial(self, trade: Trade, strategy: StrategyConfig | None, profile) -> None:
        original_qty = trade.original_qty or trade.qty
        partial_qty = first_partial_quantity(
            original_qty,
            trade.qty,
            profile.partial_take_profit_fraction,
        )
        if partial_qty <= 0 or partial_qty >= trade.qty:
            return
        fill = self.broker.close_long(
            trade.product_id,
            partial_qty,
            reference_price=trade.take_profit,
        )
        economics = first_partial_economics(
            entry_price=trade.entry_price,
            target_price=fill.price,
            original_qty=original_qty,
            current_qty=fill.qty,
            fraction=1.0,
            total_entry_fee=trade.entry_fee_paid,
            modeled_exit_net_proceeds=fill.price * fill.qty - fill.fee,
        )
        trade.partial_realized_pnl = economics.net_profit
        trade.fees_paid += fill.fee
        trade.qty = max(trade.qty - fill.qty, 0.0)
        trade.partial_taken = True
        trade.highest_price = max(trade.highest_price, fill.price)
        quote_reader = getattr(self.broker, "execution_quote", None)
        try:
            quote = (
                quote_reader(trade.product_id, fill.price)
                if callable(quote_reader)
                else conservative_quote(
                    trade.product_id,
                    fill.price,
                    self.market_cfg.paper_max_spread_bps,
                )
            )
        except Exception as exc:  # noqa: BLE001 - partial already filled; preserve protection
            log.warning(
                "breakeven quote unavailable for %s after partial; using max spread: %s",
                trade.product_id,
                exc,
            )
            quote = conservative_quote(
                trade.product_id,
                fill.price,
                self.market_cfg.paper_max_spread_bps,
            )
        remaining_entry_fee = trade.entry_fee_paid * (trade.qty / original_qty)
        entry_fee_per_unit = remaining_entry_fee / trade.qty
        costs = ExecutionCostEstimator(
            (
                strategy.assumed_fee_pct_per_side
                if strategy is not None
                else profile.assumed_fee_pct_per_side
                if hasattr(profile, "assumed_fee_pct_per_side")
                else 0.006
            ),
            self.market_cfg,
            universe=self.universe,
        )
        breakeven_floor = costs.cost_adjusted_breakeven(
            trade.entry_price,
            entry_fee_per_unit,
            quote,
        )
        trade.trailing_stop = max(trade.stop_loss, round(breakeven_floor, 8))
        trade.trailing_stop = self._chandelier_stop(trade, profile)
        self.store.update_trade(trade)
        log.info(
            "PARTIAL[%s] %s qty=%.8f exit=%.6f pnl=$%.2f trail=%.6f",
            trade.strategy,
            trade.ticker,
            fill.qty,
            fill.price,
            trade.partial_realized_pnl,
            trade.trailing_stop,
        )
        if self.trade_alerts.on_close:
            self._notify(
                *trade_partial_alert(
                    trade,
                    fill.qty,
                    fill.price,
                    trade.partial_realized_pnl,
                )
            )

    def _manage_trade_by_quote(
        self,
        trade: Trade,
        strategy: StrategyConfig | None,
        _tstop,
    ) -> None:
        """Fail-safe exit management when a complete PAPER bar walk is unavailable."""
        try:
            price = self.broker.current_price(trade.product_id)
        except Exception as exc:  # noqa: BLE001 - leave position open, retry next loop
            log.warning("Exit quote unavailable for %s: %s", trade.product_id, exc)
            return

        profile = self._profile_for(trade)
        trade.highest_price = max(trade.highest_price or trade.entry_price, price)
        partial_trail = (
            profile.advanced_exit_enabled
            and self.broker.name == "paper"
            and (not trade.exit_snapshot or trade.setup not in ("", "offline_social"))
        )
        if partial_trail:
            step = quote_step(
                profile,
                price=price,
                stop_loss=trade.stop_loss,
                take_profit=trade.take_profit,
                highest_price=trade.highest_price or trade.entry_price,
                trailing_stop=trade.trailing_stop,
                partial_taken=bool(trade.partial_taken),
                original_qty=trade.original_qty or trade.qty,
                current_qty=trade.qty,
                atr_absolute=(self._trail_atr(trade, profile) if trade.partial_taken else 0.0),
            )
            trade.highest_price = step.highest_price
            trade.trailing_stop = step.trailing_stop
            if step.actions:
                action = step.actions[0]
                if action.kind == ExitActionKind.PARTIAL:
                    self._take_partial(trade, strategy, profile)
                else:
                    self._close(trade, action.price, ExitReason(action.reason))
                return
        elif not self.broker.server_side_brackets:
            # Bounded targets retain the established simulated bracket behavior.
            step = quote_step(
                {**profile.snapshot(), "advanced_exit_enabled": False},
                price=price,
                stop_loss=trade.stop_loss,
                take_profit=trade.take_profit,
                highest_price=trade.highest_price or trade.entry_price,
                trailing_stop=trade.trailing_stop,
                partial_taken=False,
                original_qty=trade.original_qty or trade.qty,
                current_qty=trade.qty,
            )
            if step.actions:
                action = step.actions[0]
                self._close(trade, action.price, ExitReason(action.reason))
                return

        now = utcnow()
        opened = (
            trade.opened_at
            if trade.opened_at.tzinfo
            else trade.opened_at.replace(tzinfo=now.tzinfo)
        )
        reason = time_exit_reason(
            profile,
            held_seconds=(now - opened).total_seconds(),
            highest_price=trade.highest_price or trade.entry_price,
            entry_price=trade.entry_price,
            initial_risk_per_unit=trade.initial_risk_per_unit,
            partial_taken=bool(trade.partial_taken),
        )
        if reason is not None:
            self._close(trade, price, ExitReason(reason))
        else:
            self.store.update_trade(trade)

    def _manage_deployed_paper_trade(
        self,
        trade: Trade,
        strategy: StrategyConfig | None,
        tstop,
    ) -> None:
        """Walk each newly closed one-minute bar exactly once, oldest-first."""
        if trade.last_processed_paper_bar_ts <= 0:
            cursor_reader = getattr(self.broker, "last_closed_bar_ts", None)
            try:
                latest = int(cursor_reader(trade.product_id)) if callable(cursor_reader) else 0
            except Exception as exc:  # noqa: BLE001 - legacy position remains protected
                log.warning(
                    "PAPER cannot initialize exit cursor for %s: %s",
                    trade.product_id,
                    exc,
                )
                self._manage_trade_by_quote(trade, strategy, tstop)
                return
            if latest <= 0:
                log.warning("PAPER cannot initialize exit cursor for %s", trade.product_id)
                self._manage_trade_by_quote(trade, strategy, tstop)
                return
            # A legacy open trade has no trustworthy minute-by-minute history.
            # Start at the latest closed bar rather than replaying pre-entry or
            # unavailable candles; subsequent bars are processed normally.
            trade.last_processed_paper_bar_ts = latest
            self.store.update_trade(trade)
            return

        bars = []
        try:
            bars = self.broker.closed_bars_since(  # type: ignore[attr-defined]
                trade.product_id,
                trade.last_processed_paper_bar_ts,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed for this position, not all
            log.warning(
                "PAPER exit bars unavailable for %s at cursor=%s: %s",
                trade.product_id,
                trade.last_processed_paper_bar_ts,
                exc,
            )
            self._manage_trade_by_quote(trade, strategy, tstop)
            return

        profile = self._profile_for(trade)
        advanced = profile.advanced_exit_enabled and (
            not trade.exit_snapshot or trade.setup not in ("", "offline_social")
        )
        for bar in bars:
            # Persist only after any required fill succeeds. A failed fill leaves
            # the database cursor unchanged so the same bar is retried.
            trade.last_processed_paper_bar_ts = bar.ts

            if advanced:
                step = bar_step(
                    profile,
                    low=bar.low,
                    high=bar.high,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit,
                    highest_price=trade.highest_price or trade.entry_price,
                    trailing_stop=trade.trailing_stop,
                    partial_taken=bool(trade.partial_taken),
                    original_qty=trade.original_qty or trade.qty,
                    current_qty=trade.qty,
                    atr_absolute=(self._trail_atr(trade, profile) if trade.partial_taken else 0.0),
                )
                trade.highest_price = step.highest_price
                trade.trailing_stop = step.trailing_stop
                if step.actions and step.actions[0].kind == ExitActionKind.CLOSE:
                    action = step.actions[0]
                    self._close(trade, action.price, ExitReason(action.reason))
                    return
                if step.actions and step.actions[0].kind == ExitActionKind.PARTIAL:
                    self._take_partial(trade, strategy, profile)
                    if trade.trailing_stop > 0 and bar.low <= trade.trailing_stop:
                        self._close(trade, trade.trailing_stop, ExitReason.TRAILING_STOP)
                        return
            else:
                step = bar_step(
                    {**profile.snapshot(), "advanced_exit_enabled": False},
                    low=bar.low,
                    high=bar.high,
                    stop_loss=trade.stop_loss,
                    take_profit=trade.take_profit,
                    highest_price=trade.highest_price or trade.entry_price,
                    trailing_stop=trade.trailing_stop,
                    partial_taken=False,
                    original_qty=trade.original_qty or trade.qty,
                    current_qty=trade.qty,
                )
                trade.highest_price = step.highest_price
                if step.actions:
                    action = step.actions[0]
                    self._close(trade, action.price, ExitReason(action.reason))
                    return

            self.store.update_trade(trade)

        now = utcnow()
        opened = (
            trade.opened_at
            if trade.opened_at.tzinfo
            else trade.opened_at.replace(tzinfo=now.tzinfo)
        )
        reason = time_exit_reason(
            profile,
            held_seconds=(now - opened).total_seconds(),
            highest_price=trade.highest_price or trade.entry_price,
            entry_price=trade.entry_price,
            initial_risk_per_unit=trade.initial_risk_per_unit,
            partial_taken=bool(trade.partial_taken),
        )
        if reason is not None:
            self._close(
                trade,
                self.broker.current_price(trade.product_id),
                ExitReason(reason),
            )

    def manage_open_trades(self, force_flatten: bool = False) -> None:
        # Kill switch flattens EVERY strategy's positions.
        for trade in self.store.open_trades():
            # Normalize tz for time-stop comparison.
            tstop = trade.time_stop_at
            tstop = tstop if tstop.tzinfo else tstop.replace(tzinfo=utcnow().tzinfo)

            if force_flatten:
                self._kill_flatten(trade)
                continue

            strategy = self._strategy_for(trade)
            deployed_paper = (
                self.broker.name == "paper"
                and not getattr(self.broker, "offline_simulation", True)
                and callable(getattr(self.broker, "closed_bars_since", None))
            )
            if deployed_paper:
                self._manage_deployed_paper_trade(trade, strategy, tstop)
                continue

            self._manage_trade_by_quote(trade, strategy, tstop)
