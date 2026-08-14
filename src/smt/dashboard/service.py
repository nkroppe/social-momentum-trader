"""Assemble dashboard payloads from Store + public market marks."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..config import (
    MarketConfig,
    Settings,
    StrategyConfig,
    UniverseConfig,
    get_market,
    get_ops,
    get_risk,
    get_security,
    get_strategies,
    get_universe,
)
from ..logging_setup import get_logger
from ..market import MarketData
from ..models import OpportunityDecision, ShadowDecision, Trade, utcnow
from ..ops.killswitch import KillSwitch
from ..ops.soak import SoakTracker
from ..store import Store
from ..trader.execution import ExecutionCostError, ExecutionCostEstimator, conservative_quote
from .schemas import (
    EquityPoint,
    ExitReasonCount,
    HealthResponse,
    OpportunitiesResponse,
    OpportunityRow,
    OverviewResponse,
    PerformanceResponse,
    PositionRow,
    PositionsResponse,
    RiskResponse,
    RiskSnapshotRow,
    ShadowResponse,
    ShadowRow,
    StrategyPerformance,
    SymbolExposure,
    TradeRow,
    TradesResponse,
)

log = get_logger("smt.dashboard")


def coinbase_equity_reader(settings: Settings) -> Callable[[], float] | None:
    """Best-effort live portfolio reader. Never raises; None means use book equity."""
    if not (settings.live and settings.coinbase_configured):
        return None
    try:
        from ..trader.coinbase import CoinbaseBroker

        broker = CoinbaseBroker(settings, get_security())
    except Exception as exc:  # noqa: BLE001
        log.warning("dashboard cannot read live Coinbase equity: %s", exc)
        return None
    return broker.portfolio_equity_usd


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


class DashboardService:
    def __init__(
        self,
        store: Store,
        settings: Settings,
        *,
        market: MarketData | None = None,
        marks: dict[str, float] | None = None,
        mark_price: Callable[[str], float | None] | None = None,
        live_equity: Callable[[], float] | None = None,
        universe: UniverseConfig | None = None,
        strategies: list[StrategyConfig] | None = None,
        market_cfg: MarketConfig | None = None,
    ):
        self.store = store
        self.settings = settings
        self.universe = universe if universe is not None else get_universe()
        self.strategies = (
            list(strategies) if strategies is not None else get_strategies().enabled()
        )
        self.risk_cfg = get_risk()
        self.security = get_security()
        self.ops = get_ops()
        self.market_cfg = market_cfg if market_cfg is not None else get_market()
        self.market = market
        self._marks = dict(marks or {})
        self._mark_price = mark_price
        self._live_equity = live_equity
        self.kill = KillSwitch(settings.kill_file)
        self.soak = SoakTracker(Path(self.ops.soak.state_file))

    def _quote_mark(self, product_id: str) -> tuple[float | None, bool]:
        if product_id in self._marks:
            return self._marks[product_id], True
        if self._mark_price is not None:
            try:
                price = self._mark_price(product_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("mark callback failed for %s: %s", product_id, exc)
                return None, False
            if price is not None and price > 0:
                return float(price), True
            return None, False
        if self.market is None:
            return None, False
        try:
            quote = self.market.quote(product_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("quote failed for %s: %s", product_id, exc)
            return None, False
        if quote is None or quote.midpoint <= 0:
            return None, False
        return quote.midpoint, True

    def mark_for(self, trade: Trade) -> tuple[float, bool]:
        price, ok = self._quote_mark(trade.product_id)
        if ok and price is not None:
            return price, True
        return float(trade.entry_price), False

    def unrealized(self, trade: Trade, mark: float) -> float:
        return (mark - trade.entry_price) * trade.qty + (trade.partial_realized_pnl or 0.0)

    def _book_equity(self, strategy: str | None = None) -> float:
        start = self.settings.paper_start_equity
        if strategy is not None:
            match = next((item for item in self.strategies if item.name == strategy), None)
            start = start * (match.allocation if match is not None else 0.0)
        eq = start + self.store.total_realized_pnl(strategy)
        for trade in self.store.open_trades(strategy):
            mark, _ = self.mark_for(trade)
            eq += (mark - trade.entry_price) * trade.qty
            eq += trade.partial_realized_pnl or 0.0
        return eq

    def _exchange_equity(self) -> float | None:
        """Live Coinbase portfolio equity, or None to use the paper book formula."""
        if self._live_equity is None:
            return None
        try:
            value = float(self._live_equity())
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to read live equity, falling back: %s", exc)
            return None
        if value <= 0:
            log.warning("live equity is not positive ($%.2f), falling back", value)
            return None
        return value

    def equity(self) -> tuple[float, float, float]:
        """Return (equity, unrealized_incl_partials, open_partial_realized).

        Live: prefer the exchange portfolio read, same as TradeManager.equity().
        Paper (or live fallback): start equity + realized + MTM + open partials.
        """
        unrealized = 0.0
        partials = 0.0
        for trade in self.store.open_trades():
            mark, _ = self.mark_for(trade)
            unrealized += (mark - trade.entry_price) * trade.qty
            partials += trade.partial_realized_pnl or 0.0
        live = self._exchange_equity()
        equity = live if live is not None else self._book_equity()
        return equity, unrealized + partials, partials

    def allocation_equity(self, strategy: StrategyConfig) -> float:
        live = self._exchange_equity()
        if live is not None:
            return live * strategy.allocation
        return self._book_equity(strategy.name)

    def _equity_curve(self) -> list[EquityPoint]:
        closed = list(self.store.closed_trades())
        running = 0.0
        points: list[EquityPoint] = [
            EquityPoint(
                t=iso(datetime(1970, 1, 1, tzinfo=UTC)) or "",
                equity=self.settings.paper_start_equity,
                realized_pnl=0.0,
            )
        ]
        if closed:
            first = _aware(closed[0].opened_at)
            points = [
                EquityPoint(
                    t=iso(first) or "",
                    equity=self.settings.paper_start_equity,
                    realized_pnl=0.0,
                )
            ]
        for trade in closed:
            running += trade.realized_pnl
            stamp = trade.closed_at or trade.opened_at
            points.append(
                EquityPoint(
                    t=iso(stamp) or "",
                    equity=self.settings.paper_start_equity + running,
                    realized_pnl=running,
                )
            )
        equity, _, _ = self.equity()
        points.append(
            EquityPoint(t=iso(utcnow()) or "", equity=equity, realized_pnl=running)
        )
        return points

    def health(self) -> HealthResponse:
        db_ok = True
        try:
            self.store.count_open_trades()
        except Exception as exc:  # noqa: BLE001
            log.warning("dashboard db health failed: %s", exc)
            db_ok = False
        min_days = self.security.min_paper_soak_days
        days = self.soak.days_elapsed()
        state = self.soak.current_state()
        return HealthResponse(
            ok=db_ok,
            db_ok=db_ok,
            mode="LIVE" if self.settings.live else "PAPER",
            live=self.settings.live,
            kill_active=self.kill.is_active(),
            soak_days=days,
            soak_min_days=min_days,
            soak_ready=self.soak.meets_minimum(min_days),
            soak_generation=state.generation if state else 0,
            soak_summary=self.soak.summary_line(min_days),
        )

    def overview(self) -> OverviewResponse:
        equity, unrealized, partials = self.equity()
        now = utcnow()
        return OverviewResponse(
            mode="LIVE" if self.settings.live else "PAPER",
            live=self.settings.live,
            start_equity=self.settings.paper_start_equity,
            equity=equity,
            realized_pnl=self.store.total_realized_pnl(),
            unrealized_pnl=unrealized,
            partial_realized_pnl=partials,
            day_realized_pnl=self.store.realized_pnl_since(now - timedelta(days=1)),
            week_realized_pnl=self.store.realized_pnl_since(now - timedelta(days=7)),
            fees_paid=self.store.total_fees_paid(),
            open_positions=self.store.count_open_trades(),
            closed_trades=len(self.store.closed_trades()),
            equity_curve=self._equity_curve(),
        )

    def positions(self) -> PositionsResponse:
        rows: list[PositionRow] = []
        for trade in self.store.open_trades():
            mark, mark_ok = self.mark_for(trade)
            mtm = (mark - trade.entry_price) * trade.qty
            basis = trade.entry_price * trade.qty
            pct = (mtm / basis) if basis else 0.0
            tp_dist = ((trade.take_profit - mark) / mark) if mark else None
            sl_floor = max(trade.stop_loss, trade.trailing_stop)
            sl_dist = ((mark - sl_floor) / mark) if mark and sl_floor > 0 else None
            rows.append(
                PositionRow(
                    id=trade.id,
                    ticker=trade.ticker,
                    strategy=trade.strategy,
                    product_id=trade.product_id,
                    is_live=trade.is_live,
                    qty=trade.qty,
                    original_qty=trade.original_qty or trade.qty,
                    entry_price=trade.entry_price,
                    entry_notional=trade.entry_notional,
                    mark=mark,
                    mark_ok=mark_ok,
                    unrealized_pnl=mtm + (trade.partial_realized_pnl or 0.0),
                    unrealized_pct=pct,
                    partial_taken=bool(trade.partial_taken),
                    partial_realized_pnl=trade.partial_realized_pnl or 0.0,
                    take_profit=trade.take_profit,
                    stop_loss=trade.stop_loss,
                    trailing_stop=trade.trailing_stop or 0.0,
                    highest_price=trade.highest_price or 0.0,
                    setup=trade.setup or "",
                    time_stop_at=iso(trade.time_stop_at),
                    opened_at=iso(trade.opened_at) or "",
                    tp_distance_pct=tp_dist,
                    sl_distance_pct=sl_dist,
                )
            )
        rows.sort(key=lambda row: (row.strategy, row.ticker, row.id))
        return PositionsResponse(positions=rows)

    def trades(
        self,
        *,
        strategy: str | None = None,
        ticker: str | None = None,
        exit_reason: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> TradesResponse:
        rows, total = self.store.closed_trades_filtered(
            strategy=strategy or None,
            ticker=ticker or None,
            exit_reason=exit_reason or None,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
        return TradesResponse(
            trades=[self._trade_row(trade) for trade in rows],
            total=total,
            limit=limit,
            offset=offset,
        )

    @staticmethod
    def _trade_row(trade: Trade) -> TradeRow:
        hold: float | None = None
        if trade.closed_at is not None:
            hold = (_aware(trade.closed_at) - _aware(trade.opened_at)).total_seconds() / 3600.0
        return TradeRow(
            id=trade.id,
            ticker=trade.ticker,
            strategy=trade.strategy,
            product_id=trade.product_id,
            is_live=trade.is_live,
            qty=trade.qty,
            original_qty=trade.original_qty or trade.qty,
            entry_price=trade.entry_price,
            entry_notional=trade.entry_notional,
            exit_price=trade.exit_price,
            exit_reason=trade.exit_reason.value,
            realized_pnl=trade.realized_pnl,
            fees_paid=trade.fees_paid,
            partial_realized_pnl=trade.partial_realized_pnl or 0.0,
            setup=trade.setup or "",
            opened_at=iso(trade.opened_at) or "",
            closed_at=iso(trade.closed_at),
            hold_hours=hold,
        )

    def performance(self) -> PerformanceResponse:
        strategies: list[StrategyPerformance] = []
        for strategy in self.strategies:
            stats = self.store.strategy_stats(strategy.name)
            strategies.append(
                StrategyPerformance(
                    strategy=strategy.name,
                    allocation=strategy.allocation,
                    alloc_equity=self.allocation_equity(strategy),
                    open_positions=int(stats["open_positions"]),
                    closed_trades=int(stats["closed_trades"]),
                    wins=int(stats["wins"]),
                    win_rate=float(stats["win_rate"]),
                    total_pnl=float(stats["total_pnl"]),
                    day_pnl=float(stats["day_pnl"]),
                    avg_hold_hours=float(stats["avg_hold_hours"]),
                    fees_paid=self.store.total_fees_paid(strategy.name),
                )
            )
        reason_pnl: dict[str, float] = {}
        reason_count: Counter[str] = Counter()
        for trade in self.store.closed_trades():
            key = trade.exit_reason.value
            reason_count[key] += 1
            reason_pnl[key] = reason_pnl.get(key, 0.0) + trade.realized_pnl
        exits = [
            ExitReasonCount(reason=reason, count=count, pnl=reason_pnl.get(reason, 0.0))
            for reason, count in reason_count.most_common()
        ]
        return PerformanceResponse(
            strategies=strategies,
            exit_reasons=exits,
            equity_curve=self._equity_curve(),
        )

    def _quote_for_heat(self, product_id: str, mark: float):
        if self.market is not None:
            try:
                quote = self.market.quote(product_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("heat quote failed for %s: %s", product_id, exc)
                quote = None
            if quote is not None:
                return quote
        return conservative_quote(product_id, mark, self.market_cfg.paper_max_spread_bps)

    def _open_heat(self, trade: Trade, mark: float) -> float:
        """Match RiskGate existing-heat: min(stop, mark) plus modeled exit costs."""
        protective_floor = max(trade.stop_loss, trade.trailing_stop)
        if protective_floor <= 0 or trade.qty <= 0:
            return 0.0
        risk_reference = min(protective_floor, mark)
        costs = ExecutionCostEstimator(
            self.risk_cfg.assumed_fee_pct_per_side,
            self.market_cfg,
            universe=self.universe,
        )
        quote = self._quote_for_heat(trade.product_id, mark)
        try:
            sell = costs.estimate_sell(
                quote,
                trade.qty,
                reference_price=risk_reference,
                projected_reference=True,
                enforce_depth=True,
            )
        except ExecutionCostError:
            quote = conservative_quote(
                trade.product_id, mark, self.market_cfg.paper_max_spread_bps
            )
            sell = costs.estimate_sell(
                quote,
                trade.qty,
                reference_price=risk_reference,
                projected_reference=True,
                enforce_depth=False,
            )
        exit_cost = risk_reference * trade.qty - sell.net_proceeds
        return max(mark - risk_reference, 0.0) * trade.qty + max(exit_cost, 0.0)

    def risk(self) -> RiskResponse:
        equity, _, _ = self.equity()
        gross = 0.0
        heat = 0.0
        micro = 0.0
        by_symbol: dict[str, float] = {}
        opens = list(self.store.open_trades())
        for trade in opens:
            mark, _ = self.mark_for(trade)
            notional = mark * trade.qty
            gross += notional
            by_symbol[trade.ticker] = by_symbol.get(trade.ticker, 0.0) + notional
            heat += self._open_heat(trade, mark)
            if self.universe.tier_of(trade.ticker) == "micro":
                micro += notional
        denom = equity if equity > 0 else 1.0
        snapshots = [
            RiskSnapshotRow(
                strategy=row.strategy,
                period=row.period,
                bucket_start=iso(row.bucket_start) or "",
                equity=row.equity,
            )
            for row in self.store.list_risk_equity_snapshots()
        ]
        return RiskResponse(
            equity=equity,
            gross_exposure=gross,
            gross_exposure_pct=gross / denom,
            max_gross_exposure_pct=self.risk_cfg.max_gross_exposure_pct,
            open_heat=heat,
            open_heat_pct=heat / denom,
            max_open_heat_pct=self.risk_cfg.max_aggregate_open_heat_pct,
            micro_exposure=micro,
            micro_exposure_pct=micro / denom,
            max_micro_exposure_pct=self.risk_cfg.max_micro_exposure_pct,
            max_combined_symbol_exposure_pct=self.risk_cfg.max_combined_symbol_exposure_pct,
            open_positions=len(opens),
            max_open_positions=self.risk_cfg.max_open_positions,
            by_symbol=[
                SymbolExposure(ticker=ticker, notional=notional, pct_of_equity=notional / denom)
                for ticker, notional in sorted(by_symbol.items(), key=lambda item: -item[1])
            ],
            snapshots=snapshots,
        )

    def opportunities(self, limit: int = 50) -> OpportunitiesResponse:
        funnel = self.store.opportunity_status_counts()
        rows = [self._opportunity_row(row) for row in self.store.recent_opportunities(limit)]
        return OpportunitiesResponse(funnel=funnel, rows=rows)

    @staticmethod
    def _opportunity_row(row: OpportunityDecision) -> OpportunityRow:
        return OpportunityRow(
            opportunity_key=row.opportunity_key,
            ticker=row.ticker,
            strategy=row.strategy,
            outcome_status=row.outcome_status,
            outcome_reason=row.outcome_reason or "",
            setup_name=row.setup_name or "",
            setup_status=row.setup_status or "",
            regime_status=row.regime_status or "",
            social_status=row.social_status or "",
            llm_status=row.llm_status or "",
            risk_status=row.risk_status or "",
            trade_id=row.trade_id,
            evaluated_at=iso(row.evaluated_at) or "",
            return_1h=row.return_1h,
            return_4h=row.return_4h,
            return_24h=row.return_24h,
        )

    def shadow(self, limit: int = 50) -> ShadowResponse:
        total, vetoes, social_counts = self.store.shadow_summary()
        rows = [_shadow_row(row) for row in self.store.recent_shadow_decisions(limit)]
        return ShadowResponse(
            total=total,
            llm_veto_count=vetoes,
            social_counts=social_counts,
            rows=rows,
        )


def _shadow_row(row: ShadowDecision) -> ShadowRow:
    return ShadowRow(
        decision_key=row.decision_key,
        ticker=row.ticker,
        strategy=row.strategy,
        setup=row.setup or "",
        social_decision=row.social_decision or "",
        social_reason=row.social_reason or "",
        llm_status=row.llm_status or "",
        llm_score=row.llm_score or 0.0,
        llm_veto=bool(row.llm_veto),
        llm_reason=row.llm_reason or "",
        risk_status=row.risk_status or "",
        trade_id=row.trade_id if row.trade_id else None,
        first_evaluated_at=iso(row.first_evaluated_at) or "",
        updated_at=iso(row.updated_at) or "",
    )
