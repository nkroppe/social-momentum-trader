"""Domain models and the SQLAlchemy ORM schema."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TradeStatus(enum.StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"


class ExitReason(enum.StrEnum):
    TAKE_PROFIT = "TAKE_PROFIT"
    TRAILING_STOP = "TRAILING_STOP"
    STOP_LOSS = "STOP_LOSS"
    ENTRY_RISK = "ENTRY_RISK"
    TIME_STOP = "TIME_STOP"
    KILL_SWITCH = "KILL_SWITCH"
    NONE = "NONE"


class SocialEvent(Base):
    """A normalized, deduped mention from a social source."""

    __tablename__ = "social_events"
    # One row per (source, post, ticker): a single tweet can mention several
    # symbols and each needs its own mention for scoring.
    __table_args__ = (
        UniqueConstraint("source", "external_id", "ticker", name="uq_source_extid_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # reddit/x/mock
    external_id: Mapped[str] = mapped_column(String(128), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    author: Mapped[str] = mapped_column(String(128), default="", index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str] = mapped_column(String(512), default="")
    weight: Mapped[float] = mapped_column(Float, default=1.0)  # source credibility weight
    # Lexicon polarity in [-1, 1]; 0.0 means no directional language.
    sentiment: Mapped[float] = mapped_column(Float, default=0.0)
    author_followers: Mapped[int] = mapped_column(Integer, default=0)
    author_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    author_following: Mapped[int] = mapped_column(Integer, default=0)
    author_posts: Mapped[int] = mapped_column(Integer, default=0)
    author_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    author_verified: Mapped[bool] = mapped_column(default=False)
    language: Mapped[str] = mapped_column(String(16), default="")
    possibly_sensitive: Mapped[bool] = mapped_column(default=False)
    is_quote: Mapped[bool] = mapped_column(default=False)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    reposts: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    quotes: Mapped[int] = mapped_column(Integer, default=0)
    bookmarks: Mapped[int] = mapped_column(Integer, default=0)
    impressions: Mapped[int] = mapped_column(Integer, default=0)
    # Hash of normalized text, for cross-poll duplicate detection.
    text_hash: Mapped[str] = mapped_column(String(32), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SocialCount(Base):
    """An uncensored X recent-count observation for one aligned window."""

    __tablename__ = "social_counts"
    __table_args__ = (
        UniqueConstraint("source", "ticker", "window_end", name="uq_social_count_window"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    query: Mapped[str] = mapped_column(String(512), default="")
    tweet_count: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    granularity: Mapped[str] = mapped_column(String(16), default="minute")
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShadowDecision(Base):
    """Stable, queryable audit of social and LLM outcomes for a price setup."""

    __tablename__ = "shadow_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    trade_id: Mapped[int | None] = mapped_column(
        Integer, default=0, nullable=True, index=True
    )
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    tier: Mapped[str] = mapped_column(String(16), default="")
    decision_mode: Mapped[str] = mapped_column(String(16), default="shadow", index=True)
    setup: Mapped[str] = mapped_column(String(64), default="")
    count_volume: Mapped[int] = mapped_column(Integer, default=0)
    engagement: Mapped[int] = mapped_column(Integer, default=0)
    social_decision: Mapped[str] = mapped_column(String(32), default="")
    social_reason: Mapped[str] = mapped_column(Text, default="")
    llm_status: Mapped[str] = mapped_column(String(32), default="")
    llm_score: Mapped[float] = mapped_column(Float, default=0.0)
    llm_veto: Mapped[bool] = mapped_column(default=False)
    llm_reason: Mapped[str] = mapped_column(Text, default="")
    llm_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    risk_status: Mapped[str] = mapped_column(String(32), default="")
    risk_reason: Mapped[str] = mapped_column(Text, default="")
    first_evaluated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, index=True
    )


class Signal(Base):
    """A momentum signal for a ticker at a point in time."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    score: Mapped[float] = mapped_column(Float)  # z-score of velocity
    mentions: Mapped[int] = mapped_column(Integer, default=0)
    sources: Mapped[int] = mapped_column(Integer, default=0)  # distinct sources
    authors: Mapped[int] = mapped_column(Integer, default=0)  # distinct accounts
    bullish_ratio: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class Trade(Base):
    """A round-trip long position (paper or live)."""

    __tablename__ = "trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16), index=True)
    # Which strategy opened this trade (e.g. "intraday" / "swing").
    strategy: Mapped[str] = mapped_column(String(16), default="intraday", index=True)
    product_id: Mapped[str] = mapped_column(String(32))
    is_live: Mapped[bool] = mapped_column(default=False)
    status: Mapped[TradeStatus] = mapped_column(
        Enum(TradeStatus), default=TradeStatus.OPEN, index=True
    )

    qty: Mapped[float] = mapped_column(Float)
    original_qty: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float] = mapped_column(Float)
    entry_notional: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
    highest_price: Mapped[float] = mapped_column(Float, default=0.0)
    initial_risk_per_unit: Mapped[float] = mapped_column(Float, default=0.0)
    partial_taken: Mapped[bool] = mapped_column(default=False)
    partial_realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    trailing_stop: Mapped[float] = mapped_column(Float, default=0.0)
    entry_fee_paid: Mapped[float] = mapped_column(Float, default=0.0)
    setup: Mapped[str] = mapped_column(String(64), default="")
    time_stop_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    exit_price: Mapped[float] = mapped_column(Float, default=0.0)
    exit_reason: Mapped[ExitReason] = mapped_column(Enum(ExitReason), default=ExitReason.NONE)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    fees_paid: Mapped[float] = mapped_column(Float, default=0.0)

    broker_entry_order_id: Mapped[str] = mapped_column(String(64), default="")
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SecurityEvent(Base):
    """Audit trail for guardrail/security events (permission checks, transfers, kills)."""

    __tablename__ = "security_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    severity: Mapped[str] = mapped_column(String(16), default="INFO")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class RiskEquitySnapshot(Base):
    """Strategy equity at a fixed UTC day/week boundary."""

    __tablename__ = "risk_equity_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "strategy",
            "period",
            "bucket_start",
            name="uq_risk_equity_strategy_period_bucket",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy: Mapped[str] = mapped_column(String(16), index=True)
    period: Mapped[str] = mapped_column(String(8), index=True)
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    equity: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
