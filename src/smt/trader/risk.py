"""Hard risk gate.

Every proposed entry passes through here. These checks are code-enforced and
cannot be overridden by the signal engine or any model. Fail-closed: any error
or ambiguity results in rejection.

Loss halts and cadence limits remain strategy-scoped. Aggregate heat and
exposure caps deliberately span every strategy.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import (
    MarketConfig,
    RiskConfig,
    SignalsConfig,
    StrategyConfig,
    UniverseConfig,
    get_market,
    get_risk,
    get_signals,
)
from ..logging_setup import get_logger
from ..market import TopOfBookQuote, horizon_volatility
from ..models import utcnow
from ..store import Store
from .execution import (
    ExecutionCostError,
    ExecutionCostEstimator,
    ExecutionEstimate,
    conservative_quote,
)
from .signals import TradeCandidate

log = get_logger("smt.risk")


@dataclass
class RiskDecision:
    approved: bool
    notional_usd: float
    reason: str
    risk_budget_usd: float = 0.0
    projection: PortfolioProjection | None = None


@dataclass(frozen=True)
class PortfolioProjection:
    equity: float
    existing_heat: float
    proposed_heat: float
    gross_exposure: float
    symbol_exposure: float
    micro_exposure: float


class RiskGate:
    def __init__(
        self,
        store: Store,
        signals: SignalsConfig | None = None,
        market_cfg: MarketConfig | None = None,
        mark_price: Callable[[str], float | None] | None = None,
        quote: Callable[[str], TopOfBookQuote | None] | None = None,
        portfolio_equity: Callable[[], float] | None = None,
        risk: RiskConfig | None = None,
        universe: UniverseConfig | None = None,
    ):
        self.store = store
        self.signals = signals if signals is not None else get_signals()
        self.market_cfg = market_cfg if market_cfg is not None else get_market()
        self.mark_price = mark_price
        self.quote = quote
        self.portfolio_equity = portfolio_equity
        self.risk = risk if risk is not None else get_risk()
        self.universe = universe

    def _costs(self, fee_pct_per_side: float) -> ExecutionCostEstimator:
        return ExecutionCostEstimator(fee_pct_per_side, self.market_cfg, universe=self.universe)

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

    def _read_mark(self, product_id: str, cache: dict[str, float]) -> float:
        cached = cache.get(product_id)
        if cached is not None:
            return cached
        if self.mark_price is None:
            raise ExecutionCostError(f"fresh mark callback unavailable for {product_id}")
        try:
            mark = self.mark_price(product_id)
        except Exception as exc:  # noqa: BLE001
            raise ExecutionCostError(f"fresh mark failed for {product_id}: {exc}") from exc
        if mark is None or mark <= 0:
            raise ExecutionCostError(f"fresh mark unavailable for {product_id}")
        cache[product_id] = mark
        return mark

    def _quote_for(self, product_id: str, mark: float) -> TopOfBookQuote:
        if self.quote is None:
            return conservative_quote(
                product_id,
                mark,
                self.market_cfg.paper_max_spread_bps,
            )
        try:
            quote = self.quote(product_id)
        except Exception as exc:  # noqa: BLE001
            raise ExecutionCostError(f"fresh quote failed for {product_id}: {exc}") from exc
        if quote is None:
            raise ExecutionCostError(f"fresh quote unavailable for {product_id}")
        return quote

    def _candidate_stop(
        self,
        candidate: TradeCandidate,
        strategy: StrategyConfig,
        entry_price: float,
    ) -> float:
        if 0 < candidate.structure_stop < entry_price:
            return candidate.structure_stop
        stop_pct = candidate.stop_pct or strategy.stop_loss_pct
        if strategy.exit_style == "atr" and candidate.atr_pct > 0:
            horizon_vol = horizon_volatility(
                candidate.atr_pct,
                strategy.time_stop_hours,
                self.market_cfg.candle_granularity_seconds,
            )
            stop_pct = horizon_vol * strategy.atr_stop_loss_mult
            stop_pct = max(strategy.atr_min_stop_pct, min(stop_pct, strategy.atr_max_stop_pct))
        return entry_price * (1.0 - stop_pct)

    def _economic_target_check(
        self,
        candidate: TradeCandidate,
        strategy: StrategyConfig,
        notional_usd: float,
        quote: TopOfBookQuote,
    ) -> tuple[RiskDecision | None, ExecutionEstimate | None]:
        costs = self._costs(strategy.assumed_fee_pct_per_side)
        try:
            buy = costs.estimate_buy(quote, notional_usd)
        except ExecutionCostError as exc:
            return RiskDecision(False, 0.0, f"execution cost model: {exc}"), None
        if not 0 < candidate.structure_stop < buy.price:
            return None, buy

        target = buy.price + (buy.price - candidate.structure_stop) * strategy.partial_take_profit_r
        partial_qty = buy.qty * strategy.partial_take_profit_fraction
        try:
            sell = costs.estimate_sell(
                quote,
                partial_qty,
                reference_price=target,
                projected_reference=True,
                enforce_depth=True,
            )
        except ExecutionCostError as exc:
            return RiskDecision(False, 0.0, f"first partial not executable: {exc}"), None

        entry_fee_share = buy.fee * strategy.partial_take_profit_fraction
        gross_profit = (target - buy.price) * partial_qty
        execution_and_fee_cost = entry_fee_share + target * partial_qty - sell.net_proceeds
        net_profit = gross_profit - execution_and_fee_cost
        if net_profit <= 0:
            return (
                RiskDecision(
                    False,
                    0.0,
                    "first partial not positively economic: "
                    f"target=${target:.8f} gross=${gross_profit:.2f} "
                    f"modeled_cost=${execution_and_fee_cost:.2f} net=${net_profit:.2f} "
                    f"spread={quote.spread_bps:.1f}bps "
                    f"slippage={self.market_cfg.paper_adverse_slippage_bps:.1f}bps "
                    f"bid_participation={sell.participation:.1%}",
                ),
                None,
            )
        return None, buy

    def _portfolio_equity_value(
        self,
        strategy: StrategyConfig,
        equity_alloc: float,
    ) -> float:
        if self.portfolio_equity is not None:
            try:
                equity = self.portfolio_equity()
            except Exception as exc:  # noqa: BLE001
                raise ExecutionCostError(f"portfolio equity unavailable: {exc}") from exc
        elif strategy.allocation > 0:
            # Focused/offline callers do not construct a manager. Production
            # supplies the global callback, so this is only a compatibility path.
            equity = equity_alloc / strategy.allocation
        else:
            raise ExecutionCostError("portfolio equity callback unavailable")
        if equity <= 0:
            raise ExecutionCostError(f"portfolio equity is not positive (${equity:.2f})")
        return equity

    def _portfolio_projection(
        self,
        candidate: TradeCandidate,
        strategy: StrategyConfig,
        notional_usd: float,
        equity_alloc: float,
        buy: ExecutionEstimate,
        candidate_mark: float,
        marks: dict[str, float],
    ) -> PortfolioProjection:
        equity = self._portfolio_equity_value(strategy, equity_alloc)
        costs = self._costs(self.risk.assumed_fee_pct_per_side)
        existing_heat = 0.0
        gross = 0.0
        symbol = 0.0
        micro = 0.0

        for trade in self.store.open_trades():
            mark = self._read_mark(trade.product_id, marks)
            quote = self._quote_for(trade.product_id, mark)
            exposure = mark * trade.qty
            gross += exposure
            if trade.ticker == candidate.ticker:
                symbol += exposure
            tier = (
                self.universe.tier_of(trade.ticker, self.signals.default_tier)
                if self.universe is not None
                else self.signals.default_tier
            )
            if tier == "micro":
                micro += exposure

            protective_floor = max(trade.stop_loss, trade.trailing_stop)
            if protective_floor <= 0:
                raise ExecutionCostError(
                    f"{trade.product_id}: open position has no protective floor"
                )
            risk_reference = min(protective_floor, mark)
            sell = costs.estimate_sell(
                quote,
                trade.qty,
                reference_price=risk_reference,
                projected_reference=True,
                enforce_depth=True,
            )
            exit_cost = risk_reference * trade.qty - sell.net_proceeds
            existing_heat += max(mark - risk_reference, 0.0) * trade.qty + max(exit_cost, 0.0)

        proposed_exposure = max(candidate_mark * buy.qty, notional_usd)
        gross += proposed_exposure
        symbol += proposed_exposure
        candidate_tier = (
            self.universe.tier_of(candidate.ticker, candidate.tier)
            if self.universe is not None
            else candidate.tier
        )
        if candidate_tier == "micro":
            micro += proposed_exposure

        stop = self._candidate_stop(candidate, strategy, buy.price)
        if stop <= 0 or stop >= buy.price:
            raise ExecutionCostError(f"{candidate.product_id}: invalid projected stop ${stop:.8f}")
        candidate_quote = self._quote_for(candidate.product_id, candidate_mark)
        stop_sell = costs.estimate_sell(
            candidate_quote,
            buy.qty,
            reference_price=stop,
            projected_reference=True,
            enforce_depth=True,
        )
        exit_cost = stop * buy.qty - stop_sell.net_proceeds
        proposed_heat = (buy.price - stop) * buy.qty + buy.fee + max(exit_cost, 0.0)
        return PortfolioProjection(
            equity=equity,
            existing_heat=existing_heat,
            proposed_heat=proposed_heat,
            gross_exposure=gross,
            symbol_exposure=symbol,
            micro_exposure=micro,
        )

    def _global_limit_rejection(
        self,
        projection: PortfolioProjection,
    ) -> RiskDecision | None:
        checks = (
            (
                "aggregate open heat",
                projection.existing_heat + projection.proposed_heat,
                self.risk.max_aggregate_open_heat_pct,
                f"existing=${projection.existing_heat:.2f} "
                f"proposed=${projection.proposed_heat:.2f}",
            ),
            (
                "gross exposure",
                projection.gross_exposure,
                self.risk.max_gross_exposure_pct,
                "",
            ),
            (
                "combined symbol exposure",
                projection.symbol_exposure,
                self.risk.max_combined_symbol_exposure_pct,
                "",
            ),
            (
                "aggregate micro exposure",
                projection.micro_exposure,
                self.risk.max_micro_exposure_pct,
                "",
            ),
        )
        for label, projected, cap_pct, detail in checks:
            limit = projection.equity * cap_pct
            if projected > limit + 1e-9:
                ratio = projected / projection.equity
                suffix = f" {detail}" if detail else ""
                return RiskDecision(
                    False,
                    0.0,
                    f"global {label}: projected=${projected:.2f} limit=${limit:.2f} "
                    f"({ratio:.2%} > {cap_pct:.2%}){suffix}",
                )
        return None

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

    def portfolio_halted(self, strategy: StrategyConfig, start_equity: float) -> tuple[bool, str]:
        """UTC day/week equity-drawdown halts, scoped to one strategy."""
        open_pnl, quote_error = self._open_pnl(strategy.name)
        if open_pnl is None:
            return True, f"loss halt conservative: {quote_error}"

        current_equity = start_equity + self.store.total_realized_pnl(strategy.name) + open_pnl
        now = utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        day_open_pnl, day_quote_error = self._open_pnl(strategy.name, day_start)
        week_open_pnl, week_quote_error = self._open_pnl(strategy.name, week_start)
        if day_open_pnl is None or week_open_pnl is None:
            return True, f"loss halt conservative: {day_quote_error or week_quote_error}"
        day_initial = (
            current_equity - self.store.realized_pnl_since(day_start, strategy.name) - day_open_pnl
        )
        week_initial = (
            current_equity
            - self.store.realized_pnl_since(week_start, strategy.name)
            - week_open_pnl
        )
        day_baseline = self.store.risk_equity_baseline(strategy.name, "day", day_start, day_initial)
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
        marks: dict[str, float] = {}

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
            try:
                mark = self._read_mark(candidate.product_id, marks)
            except ExecutionCostError as exc:
                return RiskDecision(False, 0.0, str(exc))
            if mark <= candidate.structure_stop:
                return RiskDecision(False, 0.0, "fresh quote is at/below structure stop")
            slippage = abs(mark - candidate.entry_price) / candidate.entry_price
            if slippage > st.entry.max_entry_slippage_pct:
                return RiskDecision(
                    False,
                    0.0,
                    f"setup stale: quote moved {slippage:.2%} "
                    f"(max {st.entry.max_entry_slippage_pct:.2%})",
                )
            candidate.entry_price = mark
            candidate.stop_pct = (mark - candidate.structure_stop) / mark

        notional, sizing_note = self.size_position(candidate, st, equity_alloc)
        if notional < st.min_order_notional_usd:
            return RiskDecision(
                False,
                0.0,
                f"position notional ${notional:.2f} < min ${st.min_order_notional_usd:.2f} "
                f"({sizing_note})",
            )

        try:
            if self.mark_price is not None:
                candidate_mark = self._read_mark(candidate.product_id, marks)
            else:
                candidate_mark = candidate.entry_price if candidate.entry_price > 0 else 1.0
            execution_quote = self._quote_for(candidate.product_id, candidate_mark)
        except ExecutionCostError as exc:
            return RiskDecision(False, 0.0, f"projected controls unavailable: {exc}")

        economic_rejection, buy = self._economic_target_check(
            candidate,
            st,
            notional,
            execution_quote,
        )
        if economic_rejection is not None:
            return economic_rejection
        if buy is None:
            return RiskDecision(False, 0.0, "execution cost model produced no entry estimate")

        try:
            projection = self._portfolio_projection(
                candidate,
                st,
                notional,
                equity_alloc,
                buy,
                candidate_mark,
                marks,
            )
        except ExecutionCostError as exc:
            return RiskDecision(False, 0.0, f"projected controls unavailable: {exc}")
        global_rejection = self._global_limit_rejection(projection)
        if global_rejection is not None:
            global_rejection.projection = projection
            return global_rejection

        return RiskDecision(
            True,
            notional,
            f"approved ({sizing_note})",
            risk_budget_usd=equity_alloc * st.risk_per_trade_pct,
            projection=projection,
        )
