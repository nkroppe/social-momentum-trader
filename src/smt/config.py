"""Configuration: environment settings + typed YAML config loaders."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root = two levels up from this file (src/smt/config.py -> repo root).
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


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

    # YouTube
    youtube_api_key: str = ""

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
    # Signal entry thresholds
    signal_min_zscore: float = 2.5
    signal_min_distinct_sources: int = 2
    signal_min_mentions: int = 8
    scorer_bucket_minutes: int = 30
    scorer_lookback_buckets: int = 8


# Maximum allowed hold before a time-stop, across any strategy.
MAX_TIME_STOP_HOURS = 72

# Fields a strategy inherits from the global RiskConfig when not overridden.
_INHERITED_FIELDS = (
    "take_profit_pct",
    "stop_loss_pct",
    "time_stop_hours",
    "signal_min_zscore",
    "signal_min_distinct_sources",
    "signal_min_mentions",
    "scorer_bucket_minutes",
    "scorer_lookback_buckets",
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

    # Signal thresholds + scorer windowing
    signal_min_zscore: float
    signal_min_distinct_sources: int
    signal_min_mentions: int
    scorer_bucket_minutes: int
    scorer_lookback_buckets: int

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


class UniverseConfig(BaseModel):
    quote_currency: str = "USD"
    symbols: dict[str, SymbolSpec] = Field(default_factory=dict)
    denylist: list[str] = Field(default_factory=list)

    def tradeable(self, ticker: str) -> bool:
        return ticker in self.symbols and ticker not in self.denylist


class RedditSource(BaseModel):
    enabled: bool = False
    subreddits: list[str] = Field(default_factory=list)
    limit_per_subreddit: int = 50


class YouTubeSource(BaseModel):
    enabled: bool = False
    channels: list[str] = Field(default_factory=list)
    queries: list[str] = Field(default_factory=list)
    max_results_per_query: int = 10
    fetch_transcripts: bool = True


class XSource(BaseModel):
    enabled: bool = False
    watch_accounts: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class MockSource(BaseModel):
    enabled: bool = True
    events_per_poll: int = 30


class SourcesConfig(BaseModel):
    poll_interval_seconds: int = 300
    reddit: RedditSource = Field(default_factory=RedditSource)
    youtube: YouTubeSource = Field(default_factory=YouTubeSource)
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
