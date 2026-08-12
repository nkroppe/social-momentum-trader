"""Configuration: environment settings + typed YAML config loaders."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (src/smt/config.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolve_config_dir() -> Path:
    """Find config/ for dev (repo), editable installs, and Docker (/app/config)."""
    if override := os.environ.get("SMT_CONFIG_DIR"):
        return Path(override)
    container = Path("/app/config")
    if (container / "sources.yaml").exists():
        return container
    local = REPO_ROOT / "config"
    if (local / "sources.yaml").exists():
        return local
    return local


CONFIG_DIR = _resolve_config_dir()


class Settings(BaseSettings):
    """Runtime settings sourced from environment / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    live: bool = False
    # Explicit acknowledgement required to actually place real orders.
    # Must equal "I_UNDERSTAND_LIVE_RISK" or the app stays in paper mode.
    live_ack: str = ""
    kill_file: str = "./control/KILL"
    database_url: str = "sqlite:///./data/smt.sqlite"

    # Starting equity used for paper sizing / loss-halt math.
    paper_start_equity: float = 10_000.0
    # Poll cadence for the main trade loop (exit checks).
    loop_interval_seconds: int = 60

    # Coinbase (trade-only key)
    coinbase_api_key: str = ""
    coinbase_api_secret: str = ""
    coinbase_portfolio_id: str = ""

    # Reddit
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "social-momentum-trader/0.1"

    # X
    x_bearer_token: str = ""
    # Dollar cap shared by recent-count requests and distinct post reads.
    x_monthly_budget_usd: float = 100.0
    x_post_read_cost_usd: float = 0.005
    x_recent_count_request_cost_usd: float = 0.005
    # Legacy read cap/cost remain supported. If X_MONTHLY_BUDGET_USD is absent
    # and X_MONTHLY_READ_BUDGET is explicitly set, the old read cap determines
    # the dollar ceiling.
    x_monthly_read_budget: int = 20_000
    x_read_cost_usd: float | None = None
    # Reads already billed this month per the X console; applied when a new
    # billing month starts so the local cap stays aligned with reality.
    x_budget_opening_reads: int = 0

    # Alerts
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_to: str = ""
    ntfy_topic_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    @property
    def coinbase_configured(self) -> bool:
        return bool(self.coinbase_api_key and self.coinbase_api_secret)

    @property
    def effective_x_post_read_cost_usd(self) -> float:
        """Prefer the explicit new price, while honoring the legacy cost env."""
        if "x_read_cost_usd" in self.model_fields_set and self.x_read_cost_usd is not None:
            return self.x_read_cost_usd
        return self.x_post_read_cost_usd

    @property
    def effective_x_monthly_budget_usd(self) -> float:
        """Translate explicitly configured legacy read caps into dollars."""
        if (
            "x_monthly_read_budget" in self.model_fields_set
            and "x_monthly_budget_usd" not in self.model_fields_set
        ):
            return self.x_monthly_read_budget * self.effective_x_post_read_cost_usd
        return self.x_monthly_budget_usd


# ----------------------------------------------------------------------------
# Typed YAML configs
# ----------------------------------------------------------------------------


class RiskConfig(BaseModel):
    max_position_pct: float = 0.10
    risk_per_trade_pct: float = 0.005
    max_aggregate_open_heat_pct: float = 0.02
    max_gross_exposure_pct: float = 0.50
    max_combined_symbol_exposure_pct: float = 0.10
    max_micro_exposure_pct: float = 0.15
    max_open_positions: int = 3
    max_trades_per_day: int = 8
    daily_loss_halt_pct: float = -0.05
    weekly_loss_halt_pct: float = -0.12
    cooldown_minutes_after_stop: int = 120
    take_profit_pct: float = 0.06
    stop_loss_pct: float = 0.03
    time_stop_hours: int = 6
    min_order_notional_usd: float = 25
    assumed_fee_pct_per_side: float = 0.006

    # Exit sizing. "atr" scales targets to each asset's own volatility so one
    # rule fits both BTC and a sub-cent token; "fixed" uses the flat pcts above.
    exit_style: str = "atr"
    atr_take_profit_mult: float = 2.0
    atr_stop_loss_mult: float = 1.0
    atr_min_stop_pct: float = 0.008
    atr_max_stop_pct: float = 0.15
    advanced_exit_enabled: bool = True
    partial_take_profit_fraction: float = 0.50
    partial_take_profit_r: float = 1.5
    chandelier_atr_mult: float = 3.0
    stale_time_stop_hours: int = 4

    # Signal entry thresholds
    signal_min_zscore: float = 2.5
    signal_min_distinct_sources: int = 1
    signal_min_distinct_authors: int = 3
    signal_min_mentions: int = 8
    signal_min_bullish_ratio: float = 0.60
    scorer_bucket_minutes: int = 30
    scorer_lookback_buckets: int = 8
    # Baseline the current bucket against the same clock window on prior days
    # once this much history exists, removing the daily social cycle.
    scorer_seasonal_days: int = 7
    scorer_seasonal_min_history_hours: int = 48

    # Price confirmation: hours of trailing return required to be positive
    # before a social spike is treated as an entry.
    confirm_lookback_hours: int = 4
    confirm_min_return_pct: float = 0.0

    @field_validator("exit_style")
    @classmethod
    def _valid_exit_style(cls, v: str) -> str:
        if v not in ("atr", "fixed"):
            raise ValueError("exit_style must be 'atr' or 'fixed'")
        return v

    @field_validator("risk_per_trade_pct")
    @classmethod
    def _fraction(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("fraction must be within 0.0..1.0")
        return v

    @field_validator(
        "max_aggregate_open_heat_pct",
        "max_gross_exposure_pct",
        "max_combined_symbol_exposure_pct",
        "max_micro_exposure_pct",
    )
    @classmethod
    def _global_risk_fraction(cls, v: float) -> float:
        if not 0.0 < v <= 1.0:
            raise ValueError("global risk fractions must be within 0.0..1.0")
        return v

    @field_validator("assumed_fee_pct_per_side")
    @classmethod
    def _valid_fee(cls, v: float) -> float:
        if not 0.0 <= v < 1.0:
            raise ValueError("assumed_fee_pct_per_side must be within 0.0..<1.0")
        return v

    @field_validator("partial_take_profit_fraction")
    @classmethod
    def _partial_fraction(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError("partial_take_profit_fraction must be within 0.0..<1.0")
        return v

    @field_validator("partial_take_profit_r", "chandelier_atr_mult")
    @classmethod
    def _positive_multiplier(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("exit multipliers must be positive")
        return v

    @model_validator(mode="after")
    def _valid_global_exposure_caps(self) -> RiskConfig:
        if self.max_aggregate_open_heat_pct > self.max_gross_exposure_pct:
            raise ValueError("aggregate open heat cannot exceed gross exposure cap")
        if self.max_combined_symbol_exposure_pct > self.max_gross_exposure_pct:
            raise ValueError("combined symbol exposure cannot exceed gross exposure cap")
        if self.max_micro_exposure_pct > self.max_gross_exposure_pct:
            raise ValueError("micro exposure cannot exceed gross exposure cap")
        return self


# Maximum allowed hold before a time-stop, across any strategy.
MAX_TIME_STOP_HOURS = 72

# Fields a strategy inherits from the global RiskConfig when not overridden.
_INHERITED_FIELDS = (
    "take_profit_pct",
    "stop_loss_pct",
    "time_stop_hours",
    "exit_style",
    "atr_take_profit_mult",
    "atr_stop_loss_mult",
    "atr_min_stop_pct",
    "atr_max_stop_pct",
    "advanced_exit_enabled",
    "partial_take_profit_fraction",
    "partial_take_profit_r",
    "chandelier_atr_mult",
    "stale_time_stop_hours",
    "signal_min_zscore",
    "signal_min_distinct_sources",
    "signal_min_distinct_authors",
    "signal_min_mentions",
    "signal_min_bullish_ratio",
    "scorer_bucket_minutes",
    "scorer_lookback_buckets",
    "scorer_seasonal_days",
    "scorer_seasonal_min_history_hours",
    "confirm_lookback_hours",
    "confirm_min_return_pct",
    "max_position_pct",
    "risk_per_trade_pct",
    "max_open_positions",
    "max_trades_per_day",
    "daily_loss_halt_pct",
    "weekly_loss_halt_pct",
    "cooldown_minutes_after_stop",
    "min_order_notional_usd",
    "assumed_fee_pct_per_side",
)


class EntryRulesConfig(BaseModel):
    """Deterministic price-action rules for one holding methodology."""

    # bull_breakout: EMA stack + RSI floor + breakout/retest/VWAP.
    # bear_rally: RISK-OFF relief setups (RSI reclaim, failed breakdown, RS bounce).
    setup_family: Literal["bull_breakout", "bear_rally"] = "bull_breakout"
    trigger_granularity_seconds: int = 900
    bias_granularity_seconds: int = 3_600
    breakout_lookback: int = 20
    structure_lookback: int = 20
    rsi_periods: int = 14
    rsi_min: float = 55.0
    rsi_oversold_max: float = 35.0
    rsi_reclaim_min: float = 45.0
    rsi_lookback_bars: int = 8
    volume_lookback: int = 20
    compression_lookback: int = 20
    compression_recent: int = 5
    compression_ratio_max: float = 0.80
    require_compression: bool = False
    retest_window: int = 3
    retest_tolerance_pct: float = 0.003
    vwap_periods: int = 32
    allow_vwap_pullback: bool = True
    require_trigger_ema_stack: bool = True
    require_bias_ema_stack: bool = True
    allow_rsi_reclaim: bool = True
    allow_failed_breakdown: bool = True
    allow_rs_bounce: bool = True
    failed_breakdown_lookback: int = 20
    rs_lookback_bars: int = 16
    rs_min_outperformance_pct: float = 0.01
    max_chase_return_pct: float = 0.05
    chase_lookback_bars: int = 4
    stop_atr_buffer: float = 0.25
    min_stop_pct: float = 0.005
    max_stop_pct: float = 0.15
    max_entry_slippage_pct: float = 0.005

    @field_validator(
        "breakout_lookback",
        "structure_lookback",
        "rsi_periods",
        "rsi_lookback_bars",
        "volume_lookback",
        "compression_lookback",
        "compression_recent",
        "retest_window",
        "vwap_periods",
        "failed_breakdown_lookback",
        "rs_lookback_bars",
        "chase_lookback_bars",
    )
    @classmethod
    def _positive_period(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("indicator periods must be positive")
        return v

    @model_validator(mode="after")
    def _valid_stops(self) -> EntryRulesConfig:
        if not 0 < self.min_stop_pct <= self.max_stop_pct:
            raise ValueError("entry stop bounds must satisfy 0 < min <= max")
        if not 0 < self.max_entry_slippage_pct <= 0.05:
            raise ValueError("max_entry_slippage_pct must be within 0..5%")
        if not 0 <= self.rsi_oversold_max <= self.rsi_reclaim_min <= 100:
            raise ValueError("rsi_oversold_max must be <= rsi_reclaim_min within 0..100")
        if self.max_chase_return_pct < 0 or self.rs_min_outperformance_pct < 0:
            raise ValueError("chase/RS thresholds must be non-negative")
        return self


class StrategyConfirmationConfig(BaseModel):
    """Optional per-strategy overrides of market.yaml confirmation gates."""

    require_above_sma: bool | None = None
    require_positive_return: bool | None = None
    min_volume_zscore: float | None = None


class StrategyConfig(BaseModel):
    """One trading methodology with its own capital slice, exits, and limits.

    Any field not set in strategies.yaml inherits from the global RiskConfig,
    so risk.yaml remains the shared-defaults / hard-caps source of truth.
    """

    name: str
    enabled: bool = True
    allocation: float = 0.5  # fraction of total equity this strategy manages
    # risk_on_only: enter only when BTC > SMA50 (default bull strategies).
    # risk_off_only: enter only when BTC <= SMA50 (bear_rally).
    # always: ignore the benchmark gate.
    regime_mode: Literal["risk_on_only", "risk_off_only", "always"] = "risk_on_only"
    # Empty lists mean no extra filter (full tradeable universe).
    allowed_tickers: list[str] = Field(default_factory=list)
    allowed_tiers: list[str] = Field(default_factory=list)
    confirmation: StrategyConfirmationConfig = Field(default_factory=StrategyConfirmationConfig)

    # Exit params
    take_profit_pct: float
    stop_loss_pct: float
    time_stop_hours: int
    exit_style: str
    atr_take_profit_mult: float
    atr_stop_loss_mult: float
    atr_min_stop_pct: float
    atr_max_stop_pct: float
    advanced_exit_enabled: bool
    partial_take_profit_fraction: float
    partial_take_profit_r: float
    chandelier_atr_mult: float
    stale_time_stop_hours: int

    # Signal thresholds + scorer windowing
    signal_min_zscore: float
    signal_min_distinct_sources: int
    signal_min_distinct_authors: int
    signal_min_mentions: int
    signal_min_bullish_ratio: float
    scorer_bucket_minutes: int
    scorer_lookback_buckets: int
    scorer_seasonal_days: int
    scorer_seasonal_min_history_hours: int

    # Price confirmation window
    confirm_lookback_hours: int
    confirm_min_return_pct: float

    # Per-strategy hard limits (independent of the other strategy)
    max_position_pct: float
    risk_per_trade_pct: float
    max_open_positions: int
    max_trades_per_day: int
    daily_loss_halt_pct: float
    weekly_loss_halt_pct: float
    cooldown_minutes_after_stop: int
    min_order_notional_usd: float
    assumed_fee_pct_per_side: float
    entry: EntryRulesConfig = Field(default_factory=EntryRulesConfig)

    @field_validator("time_stop_hours")
    @classmethod
    def _cap_time_stop(cls, v: int) -> int:
        if v <= 0 or v > MAX_TIME_STOP_HOURS:
            raise ValueError(f"time_stop_hours must be in 1..{MAX_TIME_STOP_HOURS}")
        return v

    @field_validator("allowed_tickers")
    @classmethod
    def _normalize_tickers(cls, values: list[str]) -> list[str]:
        return [str(value).strip().upper() for value in values if str(value).strip()]

    @field_validator("allowed_tiers")
    @classmethod
    def _normalize_tiers(cls, values: list[str]) -> list[str]:
        return [str(value).strip().lower() for value in values if str(value).strip()]

    @model_validator(mode="after")
    def _valid_exit_times(self) -> StrategyConfig:
        if self.stale_time_stop_hours <= 0:
            raise ValueError("stale_time_stop_hours must be positive")
        if self.stale_time_stop_hours > self.time_stop_hours:
            raise ValueError("stale_time_stop_hours cannot exceed time_stop_hours")
        if not 0 < self.risk_per_trade_pct <= 1:
            raise ValueError("risk_per_trade_pct must be within 0.0..1.0")
        if not 0 < self.partial_take_profit_fraction < 1:
            raise ValueError("partial_take_profit_fraction must be within 0.0..<1.0")
        if self.partial_take_profit_r <= 0 or self.chandelier_atr_mult <= 0:
            raise ValueError("advanced exit multipliers must be positive")
        return self

    @field_validator("allocation")
    @classmethod
    def _valid_allocation(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("allocation must be within 0.0..1.0")
        return v

    @field_validator("exit_style")
    @classmethod
    def _valid_strategy_exit_style(cls, v: str) -> str:
        if v not in ("atr", "fixed"):
            raise ValueError("exit_style must be 'atr' or 'fixed'")
        return v

    def regime_allows_entries(self, risk_on: bool, *, risk_off: bool = False) -> bool:
        """Whether the BTC SMA regime state permits new entries for this strategy.

        ``risk_on`` is the full bull gate (above SMA + structure). ``risk_off`` is
        specifically daily close at/below SMA — not merely ``not risk_on``, which
        also covers structure blocks and missing history.
        """
        if self.regime_mode == "always":
            return True
        if self.regime_mode == "risk_off_only":
            return risk_off
        return risk_on


class StrategiesConfig(BaseModel):
    strategies: dict[str, StrategyConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_total_allocation(self) -> StrategiesConfig:
        total = sum(s.allocation for s in self.strategies.values() if s.enabled)
        if total > 1.0 + 1e-9:
            raise ValueError(
                f"enabled strategy allocations sum to {total:.3f} (> 1.0). "
                "Reduce allocation fractions in config/strategies.yaml."
            )
        return self

    def enabled(self) -> list[StrategyConfig]:
        return [s for s in self.strategies.values() if s.enabled]


class SymbolSpec(BaseModel):
    product_id: str
    aliases: list[str] = Field(default_factory=list)
    # Liquidity/market-cap bucket; selects a tier profile from signals.yaml.
    tier: str = "mid"
    # Only count the ticker symbol when written as a `$` cashtag. Needed for
    # symbols that collide with ordinary words ("cap", "pump"), which would
    # otherwise read every "market cap" as attention on the token. Descriptive
    # aliases ("pumpfun") are unambiguous and still match as plain text.
    require_cashtag: bool = False


class UniverseConfig(BaseModel):
    quote_currency: str = "USD"
    symbols: dict[str, SymbolSpec] = Field(default_factory=dict)
    denylist: list[str] = Field(default_factory=list)

    def tradeable(self, ticker: str) -> bool:
        return ticker in self.symbols and ticker not in self.denylist

    def tier_of(self, ticker: str, default: str = "mid") -> str:
        spec = self.symbols.get(ticker)
        return spec.tier if spec else default


class RedditSource(BaseModel):
    enabled: bool = False
    subreddits: list[str] = Field(default_factory=list)
    limit_per_subreddit: int = 50


class XSource(BaseModel):
    enabled: bool = False
    watch_accounts: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    max_results_per_query: int = 100
    counts_enabled: bool = False
    count_window_minutes: int = 30
    count_granularity: Literal["minute"] = "minute"
    trigger_min_count: int = 8
    trigger_zscore: float = 2.0
    trigger_relative_multiple: float = 2.0
    trigger_min_baseline_windows: int = 24
    cold_start_sample_interval: int = 12
    sample_size: int = 25
    mention_weight: float = 2.0
    # Ask X to exclude retweets/replies server-side so budget is spent on
    # original posts rather than amplification noise.
    exclude_retweets: bool = True
    exclude_replies: bool = True

    @field_validator("max_results_per_query")
    @classmethod
    def _valid_max_results(cls, v: int) -> int:
        # X recent-search accepts 10..100.
        return max(10, min(v, 100))

    @field_validator("sample_size")
    @classmethod
    def _valid_sample_size(cls, v: int) -> int:
        return max(10, min(v, 100))

    @field_validator(
        "count_window_minutes",
        "trigger_min_count",
        "trigger_min_baseline_windows",
        "cold_start_sample_interval",
    )
    @classmethod
    def _positive_count_setting(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("X count window and trigger settings must be positive")
        return v


class MockSource(BaseModel):
    enabled: bool = True
    events_per_poll: int = 30


class SourcesConfig(BaseModel):
    poll_interval_seconds: int = 300
    reddit: RedditSource = Field(default_factory=RedditSource)
    x: XSource = Field(default_factory=XSource)
    mock: MockSource = Field(default_factory=MockSource)


class TransferMonitorConfig(BaseModel):
    enabled: bool = True
    poll_interval_seconds: int = 300


class SecurityConfig(BaseModel):
    require_trade_only_key: bool = True
    forbid_transfer_permission: bool = True
    allowed_api_path_prefixes: list[str] = Field(default_factory=list)
    forbidden_api_path_substrings: list[str] = Field(default_factory=list)
    transfer_monitor: TransferMonitorConfig = Field(default_factory=TransferMonitorConfig)
    balance_anomaly_drop_pct: float = 0.10
    min_paper_soak_days: int = 14


class SoakOpsConfig(BaseModel):
    state_file: str = "./data/soak.json"
    digest_interval_hours: int = 24


class PreflightConfig(BaseModel):
    require_reddit: bool = True
    require_x: bool = True
    require_alert_channel: bool = True
    require_postgres: bool = True


class TradeAlertsConfig(BaseModel):
    """Per-trade notifications on entry and exit."""

    enabled: bool = True
    on_open: bool = True
    on_close: bool = True


class TelegramControlConfig(BaseModel):
    """Inbound Telegram commands that trip or clear the kill switch."""

    enabled: bool = True
    state_file: str = "./data/telegram_control.json"
    request_timeout_seconds: float = 5.0

    @field_validator("request_timeout_seconds")
    @classmethod
    def _positive_timeout(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        return value


WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


class WeeklyReportConfig(BaseModel):
    """Scheduled weekly performance summary, sent on wall-clock local time."""

    enabled: bool = True
    # IANA zone, so the send time tracks daylight saving automatically.
    timezone: str = "America/New_York"
    weekday: str = "sunday"
    hour: int = 20
    minute: int = 0
    state_file: str = "./data/weekly_report.json"
    # Cap the per-trade list; the rest are summarized as a count.
    max_trades_listed: int = 40

    @field_validator("weekday")
    @classmethod
    def _valid_weekday(cls, v: str) -> str:
        name = v.strip().lower()
        if name not in WEEKDAYS:
            raise ValueError(f"weekday must be one of {', '.join(WEEKDAYS)}")
        return name

    @field_validator("hour")
    @classmethod
    def _valid_hour(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError("hour must be between 0 and 23")
        return v

    @field_validator("minute")
    @classmethod
    def _valid_minute(cls, v: int) -> int:
        if not 0 <= v <= 59:
            raise ValueError("minute must be between 0 and 59")
        return v

    @property
    def weekday_index(self) -> int:
        """Monday=0, matching datetime.weekday()."""
        return WEEKDAYS.index(self.weekday)


class ShadowReportConfig(BaseModel):
    """Conservative evidence floors for staged shadow activation reviews."""

    report_days: int = 28
    min_observation_days: int = 28
    min_count_coverage: float = 0.95
    min_closed_linked_trades_per_tier: int = 30
    min_completed_per_outcome_group: int = 10
    min_llm_completion_rate: float = 0.95
    max_llm_error_rate: float = 0.05
    min_expectancy_separation_r: float = 0.25

    @field_validator(
        "report_days",
        "min_observation_days",
        "min_closed_linked_trades_per_tier",
        "min_completed_per_outcome_group",
    )
    @classmethod
    def _positive_readiness_count(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("shadow report readiness counts must be positive")
        return value

    @field_validator("min_count_coverage", "min_llm_completion_rate", "max_llm_error_rate")
    @classmethod
    def _readiness_fraction(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("shadow report fractions must be within 0..1")
        return value

    @field_validator("min_expectancy_separation_r")
    @classmethod
    def _positive_expectancy_gap(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("shadow expectancy separation must be positive")
        return value


class OpsConfig(BaseModel):
    soak: SoakOpsConfig = Field(default_factory=SoakOpsConfig)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)
    trade_alerts: TradeAlertsConfig = Field(default_factory=TradeAlertsConfig)
    telegram_control: TelegramControlConfig = Field(default_factory=TelegramControlConfig)
    weekly_report: WeeklyReportConfig = Field(default_factory=WeeklyReportConfig)
    shadow_report: ShadowReportConfig = Field(default_factory=ShadowReportConfig)


# ----------------------------------------------------------------------------
# Market data / price confirmation
# ----------------------------------------------------------------------------


class ConfirmationConfig(BaseModel):
    """Price + volume confirmation applied on top of a social signal."""

    enabled: bool = True
    # No market data -> no entry. Social attention alone is not evidence.
    fail_closed: bool = True
    require_above_sma: bool = True
    sma_periods: int = 24
    require_positive_return: bool = True
    min_volume_zscore: float = 0.0
    volume_periods: int = 24


class RegimeConfig(BaseModel):
    """Benchmark trend filter: block new longs in a broad downtrend.

    RISK-ON requires the daily close above SMA(sma_periods). When
    ``require_no_lower_lows`` is set, consecutive lower lows on the structure
    timeframe (default 4h) also block RISK-ON — so alts are not bought into a
    BTC breakdown that the daily SMA has not yet flipped.
    """

    enabled: bool = True
    benchmark_product_id: str = "BTC-USD"
    granularity_seconds: int = 86_400
    sma_periods: int = 50
    fail_closed: bool = True
    require_no_lower_lows: bool = True
    structure_granularity_seconds: int = 14_400  # 4h
    structure_lower_lows_bars: int = 3

    @field_validator("structure_granularity_seconds", "structure_lower_lows_bars", "sma_periods")
    @classmethod
    def _positive_regime_periods(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("regime period settings must be positive")
        return value

    @model_validator(mode="after")
    def _valid_lower_lows(self) -> RegimeConfig:
        if self.require_no_lower_lows and self.structure_lower_lows_bars < 2:
            raise ValueError("structure_lower_lows_bars must be >= 2 when enabled")
        return self


class VolSizingConfig(BaseModel):
    """Scale notional down for high-volatility assets.

    Scale is capped at 1.0 so this can only ever reduce exposure below the hard
    max_position_pct limit, never raise it.
    """

    enabled: bool = True
    target_atr_pct: float = 0.02
    min_scale: float = 0.25
    max_scale: float = 1.0


class MarketConfig(BaseModel):
    candle_granularity_seconds: int = 3_600
    atr_periods: int = 14
    cache_ttl_seconds: int = 300
    price_cache_ttl_seconds: int = 2
    request_timeout_seconds: float = 15.0
    unavailable_retry_seconds: int = 900
    # Paper fills price off real Coinbase quotes so soak results reflect the
    # same market the live path would trade.
    paper_use_real_prices: bool = True
    paper_quote_max_age_seconds: float = 10.0
    paper_bar_granularity_seconds: int = 60
    paper_bar_max_age_seconds: float = 300.0
    paper_bar_cache_ttl_seconds: float = 10.0
    # Coinbase omits empty 1m slots on thin books. Fill short holes with flat
    # zero-volume bars so PAPER can walk time; longer holes still fail closed.
    paper_bar_gap_fill_enabled: bool = True
    paper_bar_gap_fill_max_bars: int = 5
    paper_max_spread_bps: float = 40.0
    paper_min_top_level_notional_usd: float = 100.0
    paper_max_top_level_participation: float = 0.50
    paper_adverse_slippage_bps: float = 5.0
    candle_max_age_multiplier: float = 1.10
    confirmation: ConfirmationConfig = Field(default_factory=ConfirmationConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    sizing: VolSizingConfig = Field(default_factory=VolSizingConfig)
    price_action_enabled: bool = True
    price_action_fail_closed: bool = True

    @model_validator(mode="after")
    def _valid_market_freshness(self) -> MarketConfig:
        if self.paper_bar_granularity_seconds != 60:
            raise ValueError("paper_bar_granularity_seconds must be 60")
        positive = {
            "price_cache_ttl_seconds": self.price_cache_ttl_seconds,
            "paper_quote_max_age_seconds": self.paper_quote_max_age_seconds,
            "paper_bar_max_age_seconds": self.paper_bar_max_age_seconds,
            "paper_bar_cache_ttl_seconds": self.paper_bar_cache_ttl_seconds,
            "paper_max_spread_bps": self.paper_max_spread_bps,
            "paper_min_top_level_notional_usd": self.paper_min_top_level_notional_usd,
            "paper_adverse_slippage_bps": self.paper_adverse_slippage_bps,
        }
        if any(value <= 0 for value in positive.values()):
            raise ValueError("paper freshness, liquidity, and slippage settings must be positive")
        if self.price_cache_ttl_seconds > self.paper_quote_max_age_seconds:
            raise ValueError("price_cache_ttl_seconds cannot exceed paper_quote_max_age_seconds")
        if self.paper_bar_cache_ttl_seconds > self.paper_bar_max_age_seconds:
            raise ValueError("paper_bar_cache_ttl_seconds cannot exceed paper_bar_max_age_seconds")
        if self.paper_bar_gap_fill_max_bars < 0:
            raise ValueError("paper_bar_gap_fill_max_bars cannot be negative")
        if not 0 < self.paper_max_top_level_participation <= 0.50:
            raise ValueError("paper_max_top_level_participation must be within 0..0.50")
        if self.paper_max_spread_bps > 1_000 or self.paper_adverse_slippage_bps > 1_000:
            raise ValueError("paper spread and slippage limits cannot exceed 1000 bps")
        if self.candle_max_age_multiplier < 1.0:
            raise ValueError("candle_max_age_multiplier must be at least 1.0")
        return self


# ----------------------------------------------------------------------------
# Social signal quality
# ----------------------------------------------------------------------------


class SpamFilterConfig(BaseModel):
    enabled: bool = True
    drop_retweets: bool = True
    max_cashtags_per_post: int = 4
    max_urls_per_post: int = 2
    min_author_followers: int = 100
    min_text_chars: int = 15
    dedup_window_minutes: int = 120
    max_posts_per_author_per_window: int = 3
    blocklist_phrases: list[str] = Field(default_factory=list)


class SentimentConfig(BaseModel):
    enabled: bool = True
    bullish_terms: list[str] = Field(default_factory=list)
    bearish_terms: list[str] = Field(default_factory=list)
    negations: list[str] = Field(default_factory=lambda: ["not", "no", "never", "isnt", "aint"])
    # Drop posts with no directional language at all.
    require_directional: bool = False


SIGNAL_MODES = ("social", "trend", "hybrid")
SOCIAL_POLICIES = ("ignored", "optional", "required", "catalyst")
RETEST_POLICIES = ("preferred", "required")


class TierConfig(BaseModel):
    """Per-liquidity-tier overrides.

    Majors are efficient enough that raw attention is weak and often contrarian,
    so they default to `trend`; micro-caps are where social genuinely leads.
    """

    signal_mode: str = "hybrid"
    zscore_mult: float = 1.0
    min_mentions_mult: float = 1.0
    min_trailing_return_pct: float = 0.0
    max_position_pct_mult: float = 1.0
    social_policy: str = "required"
    min_relative_volume: float = 2.0
    retest_policy: str = "required"
    allow_vwap_pullback: bool = False
    optional_social_boost: float = 1.10
    social_veto_bullish_ratio: float = 0.40

    @field_validator("signal_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in SIGNAL_MODES:
            raise ValueError(f"signal_mode must be one of {SIGNAL_MODES}")
        return v

    @field_validator("social_policy")
    @classmethod
    def _valid_social_policy(cls, v: str) -> str:
        if v not in SOCIAL_POLICIES:
            raise ValueError(f"social_policy must be one of {SOCIAL_POLICIES}")
        return v

    @field_validator("retest_policy")
    @classmethod
    def _valid_retest_policy(cls, v: str) -> str:
        if v not in RETEST_POLICIES:
            raise ValueError(f"retest_policy must be one of {RETEST_POLICIES}")
        return v


class SignalsConfig(BaseModel):
    default_tier: str = "mid"
    social_decision_mode: Literal["shadow", "enforce"] = "shadow"
    # Minimum posts carrying directional language before the bullish ratio is
    # trusted; below this the ratio is too noisy to gate on.
    min_sentiment_posts: int = 5
    spam: SpamFilterConfig = Field(default_factory=SpamFilterConfig)
    sentiment: SentimentConfig = Field(default_factory=SentimentConfig)
    tiers: dict[str, TierConfig] = Field(default_factory=dict)

    def tier(self, name: str) -> TierConfig:
        return self.tiers.get(name) or self.tiers.get(self.default_tier) or TierConfig()


# Must match LIVE_ACK env value for live trading.
LIVE_ACK_PHRASE = "I_UNDERSTAND_LIVE_RISK"


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


@lru_cache(maxsize=1)
def get_risk() -> RiskConfig:
    return RiskConfig(**_load_yaml("risk.yaml"))


@lru_cache(maxsize=1)
def get_universe() -> UniverseConfig:
    return UniverseConfig(**_load_yaml("universe.yaml"))


@lru_cache(maxsize=1)
def get_sources() -> SourcesConfig:
    return SourcesConfig(**_load_yaml("sources.yaml"))


@lru_cache(maxsize=1)
def get_security() -> SecurityConfig:
    return SecurityConfig(**_load_yaml("security.yaml"))


@lru_cache(maxsize=1)
def get_ops() -> OpsConfig:
    return OpsConfig(**_load_yaml("ops.yaml"))


@lru_cache(maxsize=1)
def get_market() -> MarketConfig:
    return MarketConfig(**_load_yaml("market.yaml"))


@lru_cache(maxsize=1)
def get_signals() -> SignalsConfig:
    return SignalsConfig(**_load_yaml("signals.yaml"))


@lru_cache(maxsize=1)
def get_strategies() -> StrategiesConfig:
    """Load strategies.yaml, inheriting unset fields from risk.yaml.

    Backward compatible: if no strategies are defined, a single `intraday`
    strategy at full allocation reproduces the pre-dual-strategy behavior.
    """
    risk = get_risk()
    inherited = {f: getattr(risk, f) for f in _INHERITED_FIELDS}

    raw = _load_yaml("strategies.yaml")
    defs = (raw or {}).get("strategies") or {}
    if not defs:
        defs = {"intraday": {"enabled": True, "allocation": 1.0}}

    strategies: dict[str, StrategyConfig] = {}
    for name, override in defs.items():
        merged: dict = {**inherited, "name": name, "enabled": True, "allocation": 0.5}
        merged.update(override or {})
        merged["name"] = name  # name is authoritative from the mapping key
        if "entry" not in merged:
            merged["entry"] = (
                EntryRulesConfig(
                    trigger_granularity_seconds=3_600,
                    bias_granularity_seconds=14_400,
                    require_compression=False,
                    allow_vwap_pullback=False,
                )
                if name == "swing"
                else EntryRulesConfig()
            )
        strategies[name] = StrategyConfig(**merged)
    return StrategiesConfig(strategies=strategies)
