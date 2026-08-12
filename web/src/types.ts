export type Health = {
  ok: boolean;
  db_ok: boolean;
  mode: string;
  live: boolean;
  kill_active: boolean;
  soak_days: number;
  soak_min_days: number;
  soak_ready: boolean;
  soak_generation: number;
  soak_summary: string;
};

export type EquityPoint = {
  t: string;
  equity: number;
  realized_pnl: number;
};

export type Overview = {
  mode: string;
  live: boolean;
  start_equity: number;
  equity: number;
  realized_pnl: number;
  unrealized_pnl: number;
  partial_realized_pnl: number;
  day_realized_pnl: number;
  week_realized_pnl: number;
  fees_paid: number;
  open_positions: number;
  closed_trades: number;
  equity_curve: EquityPoint[];
};

export type Position = {
  id: number;
  ticker: string;
  strategy: string;
  product_id: string;
  is_live: boolean;
  qty: number;
  original_qty: number;
  entry_price: number;
  entry_notional: number;
  mark: number;
  mark_ok: boolean;
  unrealized_pnl: number;
  unrealized_pct: number;
  partial_taken: boolean;
  partial_realized_pnl: number;
  take_profit: number;
  stop_loss: number;
  trailing_stop: number;
  highest_price: number;
  setup: string;
  time_stop_at: string | null;
  opened_at: string;
  tp_distance_pct: number | null;
  sl_distance_pct: number | null;
};

export type Trade = {
  id: number;
  ticker: string;
  strategy: string;
  product_id: string;
  is_live: boolean;
  qty: number;
  original_qty: number;
  entry_price: number;
  entry_notional: number;
  exit_price: number;
  exit_reason: string;
  realized_pnl: number;
  fees_paid: number;
  partial_realized_pnl: number;
  setup: string;
  opened_at: string;
  closed_at: string | null;
  hold_hours: number | null;
};

export type TradesPage = {
  trades: Trade[];
  total: number;
  limit: number;
  offset: number;
};

export type StrategyPerformance = {
  strategy: string;
  allocation: number;
  alloc_equity: number;
  open_positions: number;
  closed_trades: number;
  wins: number;
  win_rate: number;
  total_pnl: number;
  day_pnl: number;
  avg_hold_hours: number;
  fees_paid: number;
};

export type ExitReasonCount = {
  reason: string;
  count: number;
  pnl: number;
};

export type Performance = {
  strategies: StrategyPerformance[];
  exit_reasons: ExitReasonCount[];
  equity_curve: EquityPoint[];
};

export type Risk = {
  equity: number;
  gross_exposure: number;
  gross_exposure_pct: number;
  max_gross_exposure_pct: number;
  open_heat: number;
  open_heat_pct: number;
  max_open_heat_pct: number;
  micro_exposure: number;
  micro_exposure_pct: number;
  max_micro_exposure_pct: number;
  max_combined_symbol_exposure_pct: number;
  open_positions: number;
  max_open_positions: number;
  by_symbol: { ticker: string; notional: number; pct_of_equity: number }[];
  snapshots: { strategy: string; period: string; bucket_start: string; equity: number }[];
};

export type Opportunity = {
  opportunity_key: string;
  ticker: string;
  strategy: string;
  outcome_status: string;
  outcome_reason: string;
  setup_name: string;
  setup_status: string;
  regime_status: string;
  social_status: string;
  llm_status: string;
  risk_status: string;
  trade_id: number | null;
  evaluated_at: string;
  return_1h: number | null;
  return_4h: number | null;
  return_24h: number | null;
};

export type Opportunities = {
  funnel: Record<string, number>;
  rows: Opportunity[];
};

export type Shadow = {
  total: number;
  llm_veto_count: number;
  social_counts: Record<string, number>;
  rows: {
    decision_key: string;
    ticker: string;
    strategy: string;
    setup: string;
    social_decision: string;
    social_reason: string;
    llm_status: string;
    llm_score: number;
    llm_veto: boolean;
    llm_reason: string;
    risk_status: string;
    trade_id: number | null;
    first_evaluated_at: string;
    updated_at: string;
  }[];
};
