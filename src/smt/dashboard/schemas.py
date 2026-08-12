"""Pydantic response models for the dashboard API."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    ok: bool
    db_ok: bool
    mode: str
    live: bool
    kill_active: bool
    soak_days: float
    soak_min_days: int
    soak_ready: bool
    soak_generation: int
    soak_summary: str


class EquityPoint(BaseModel):
    t: str
    equity: float
    realized_pnl: float


class OverviewResponse(BaseModel):
    mode: str
    live: bool
    start_equity: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    partial_realized_pnl: float
    day_realized_pnl: float
    week_realized_pnl: float
    fees_paid: float
    open_positions: int
    closed_trades: int
    equity_curve: list[EquityPoint] = Field(default_factory=list)


class PositionRow(BaseModel):
    id: int
    ticker: str
    strategy: str
    product_id: str
    is_live: bool
    qty: float
    original_qty: float
    entry_price: float
    entry_notional: float
    mark: float
    mark_ok: bool
    unrealized_pnl: float
    unrealized_pct: float
    partial_taken: bool
    partial_realized_pnl: float
    take_profit: float
    stop_loss: float
    trailing_stop: float
    highest_price: float
    setup: str
    time_stop_at: str | None
    opened_at: str
    tp_distance_pct: float | None
    sl_distance_pct: float | None


class PositionsResponse(BaseModel):
    positions: list[PositionRow]


class TradeRow(BaseModel):
    id: int
    ticker: str
    strategy: str
    product_id: str
    is_live: bool
    qty: float
    original_qty: float
    entry_price: float
    entry_notional: float
    exit_price: float
    exit_reason: str
    realized_pnl: float
    fees_paid: float
    partial_realized_pnl: float
    setup: str
    opened_at: str
    closed_at: str | None
    hold_hours: float | None


class TradesResponse(BaseModel):
    trades: list[TradeRow]
    total: int
    limit: int
    offset: int


class ExitReasonCount(BaseModel):
    reason: str
    count: int
    pnl: float


class StrategyPerformance(BaseModel):
    strategy: str
    allocation: float
    alloc_equity: float
    open_positions: int
    closed_trades: int
    wins: int
    win_rate: float
    total_pnl: float
    day_pnl: float
    avg_hold_hours: float
    fees_paid: float


class PerformanceResponse(BaseModel):
    strategies: list[StrategyPerformance]
    exit_reasons: list[ExitReasonCount]
    equity_curve: list[EquityPoint]


class SymbolExposure(BaseModel):
    ticker: str
    notional: float
    pct_of_equity: float


class RiskSnapshotRow(BaseModel):
    strategy: str
    period: str
    bucket_start: str
    equity: float


class RiskResponse(BaseModel):
    equity: float
    gross_exposure: float
    gross_exposure_pct: float
    max_gross_exposure_pct: float
    open_heat: float
    open_heat_pct: float
    max_open_heat_pct: float
    micro_exposure: float
    micro_exposure_pct: float
    max_micro_exposure_pct: float
    max_combined_symbol_exposure_pct: float
    open_positions: int
    max_open_positions: int
    by_symbol: list[SymbolExposure]
    snapshots: list[RiskSnapshotRow]


class OpportunityRow(BaseModel):
    opportunity_key: str
    ticker: str
    strategy: str
    outcome_status: str
    outcome_reason: str
    setup_name: str
    setup_status: str
    regime_status: str
    social_status: str
    llm_status: str
    risk_status: str
    trade_id: int | None
    evaluated_at: str
    return_1h: float | None
    return_4h: float | None
    return_24h: float | None


class OpportunitiesResponse(BaseModel):
    funnel: dict[str, int]
    rows: list[OpportunityRow]


class ShadowRow(BaseModel):
    decision_key: str
    ticker: str
    strategy: str
    setup: str
    social_decision: str
    social_reason: str
    llm_status: str
    llm_score: float
    llm_veto: bool
    llm_reason: str
    risk_status: str
    trade_id: int | None
    first_evaluated_at: str
    updated_at: str


class ShadowResponse(BaseModel):
    total: int
    llm_veto_count: int
    social_counts: dict[str, int]
    rows: list[ShadowRow]
