"""Configuration: environment settings + typed YAML config loaders."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

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
    paper_start_equity: float = 5000.0
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
    x_monthly_read_budget: int = 50_000

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


# ----------------------------------------------------------------------------
# Typed YAML configs
# ----------------------------------------------------------------------------


class RiskConfig(BaseModel):
    max_position_pct: float = 0.10
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
    "max_open_positions",
    "max_trades_per_day",
    "daily_loss_halt_pct",
    "weekly_loss_halt_pct",
    "cooldown_minutes_after_stop",
    "min_order_notional_usd",
    "assumed_fee_pct_per_side",
)


class StrategyConfig(BaseModel):
    """One trading methodology with its own capital slice, exits, and limits.

    Any field not set in strategies.yaml inherits from the global RiskConfig,
    so risk.yaml remains the shared-defaults / hard-caps source of truth.
    """

    name: str
    enabled: bool = True
    allocation: float = 0.5  # fraction of total equity this strategy manages

    # Exit params
    take_profit_pct: float
    stop_loss_pct: float
    time_stop_hours: int
    exit_style: str
    atr_take_profit_mult: float
    atr_stop_loss_mult: float
    atr_min_stop_pct: float
    atr_max_stop_pct: float

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
    max_open_positions: int
    max_trades_per_day: int
    daily_loss_halt_pct: float
    weekly_loss_halt_pct: float
    cooldown_minutes_after_stop: int
    min_order_notional_usd: float
    assumed_fee_pct_per_side: float

    @field_validator("time_stop_hours")
    @classmethod
    def _cap_time_stop(cls, v: int) -> int:
        if v <= 0 or v > MAX_TIME_STOP_HOURS:
            raise ValueError(f"time_stop_hours must be in 1..{MAX_TIME_STOP_HOURS}")
        return v

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


class OpsConfig(BaseModel):
    soak: SoakOpsConfig = Field(default_factory=SoakOpsConfig)
    preflight: PreflightConfig = Field(default_factory=PreflightConfig)
    trade_alerts: TradeAlertsConfig = Field(default_factory=TradeAlertsConfig)
    weekly_report: WeeklyReportConfig = Field(default_factory=WeeklyReportConfig)


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
    """Benchmark trend filter: block new longs in a broad downtrend."""

    enabled: bool = True
    benchmark_product_id: str = "BTC-USD"
    granularity_seconds: int = 86_400
    sma_periods: int = 50
    fail_closed: bool = True


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
    price_cache_ttl_seconds: int = 20
    request_timeout_seconds: float = 15.0
    unavailable_retry_seconds: int = 900
    # Paper fills price off real Coinbase quotes so soak results reflect the
    # same market the live path would trade.
    paper_use_real_prices: bool = True
    confirmation: ConfirmationConfig = Field(default_factory=ConfirmationConfig)
    regime: RegimeConfig = Field(default_factory=RegimeConfig)
    sizing: VolSizingConfig = Field(default_factory=VolSizingConfig)


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

    @field_validator("signal_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in SIGNAL_MODES:
            raise ValueError(f"signal_mode must be one of {SIGNAL_MODES}")
        return v


class SignalsConfig(BaseModel):
    default_tier: str = "mid"
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
        strategies[name] = StrategyConfig(**merged)
    return StrategiesConfig(strategies=strategies)
