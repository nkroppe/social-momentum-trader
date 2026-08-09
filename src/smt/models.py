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
    STOP_LOSS = "STOP_LOSS"
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
    # Hash of normalized text, for cross-poll duplicate detection.
    text_hash: Mapped[str] = mapped_column(String(32), default="", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    entry_price: Mapped[float] = mapped_column(Float)
    entry_notional: Mapped[float] = mapped_column(Float)
    take_profit: Mapped[float] = mapped_column(Float)
    stop_loss: Mapped[float] = mapped_column(Float)
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
