"""Persistence layer: engine, session, and typed helper queries."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .logging_setup import get_logger
from .models import Base, ExitReason, SecurityEvent, Signal, SocialEvent, Trade, TradeStatus, utcnow

log = get_logger("smt.store")

# Columns added after the initial schema; applied idempotently on init_db().
_SOCIAL_EVENT_COLUMNS = (
    ("sentiment", "FLOAT DEFAULT 0.0"),
    ("author_followers", "INTEGER DEFAULT 0"),
    ("text_hash", "VARCHAR(32) DEFAULT ''"),
)
_SIGNAL_COLUMNS = (
    ("authors", "INTEGER DEFAULT 0"),
    ("bullish_ratio", "FLOAT DEFAULT 0.0"),
)


@dataclass(frozen=True)
class MentionStats:
    mentions: int
    sources: int
    authors: int
    weighted: float
    bullish: int
    bearish: int

    @property
    def directional(self) -> int:
        return self.bullish + self.bearish

    @property
    def bullish_ratio(self) -> float:
        """Share of directional posts that are bullish; 0.0 when none are."""
        return (self.bullish / self.directional) if self.directional else 0.0


class Store:
    def __init__(self, database_url: str):
        self.database_url = database_url
        # Ensure sqlite parent dir exists.
        if database_url.startswith("sqlite:///"):
            db_path = database_url.replace("sqlite:///", "", 1)
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(database_url, future=True)
        self._Session = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def init_db(self) -> None:
        Base.metadata.create_all(self.engine)
        self._migrate()
        log.info("Database ready at %s", self.database_url)

    def _migrate(self) -> None:
        """Lightweight, idempotent schema migrations for existing dev DBs.

        create_all() only creates missing tables, not new columns on existing
        ones, so each schema addition needs an explicit ALTER here.
        """
        self._ensure_trade_strategy_column()
        self._ensure_columns("social_events", _SOCIAL_EVENT_COLUMNS)
        self._ensure_columns("signals", _SIGNAL_COLUMNS)

    def _ensure_columns(self, table: str, columns: tuple[tuple[str, str], ...]) -> None:
        dialect = self.engine.dialect.name
        with self.engine.begin() as conn:
            if dialect == "sqlite":
                existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
                if not existing:
                    return
                for name, ddl in columns:
                    if name not in existing:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                        log.info("migrated: added %s.%s", table, name)
            else:
                for name, ddl in columns:
                    conn.execute(
                        text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}")
                    )

    def _ensure_trade_strategy_column(self) -> None:
        dialect = self.engine.dialect.name
        with self.engine.begin() as conn:
            if dialect == "sqlite":
                cols = {row[1] for row in conn.execute(text("PRAGMA table_info(trades)"))}
                if "trades" in Base.metadata.tables and "strategy" not in cols and cols:
                    conn.execute(
                        text(
                            "ALTER TABLE trades ADD COLUMN "
                            "strategy VARCHAR(16) DEFAULT 'intraday'"
                        )
                    )
                    conn.execute(
                        text("UPDATE trades SET strategy='intraday' WHERE strategy IS NULL")
                    )
                    log.info("migrated: added trades.strategy column (sqlite)")
            else:
                # Postgres / others that support IF NOT EXISTS.
                conn.execute(
                    text(
                        "ALTER TABLE trades ADD COLUMN IF NOT EXISTS "
                        "strategy VARCHAR(16) DEFAULT 'intraday'"
                    )
                )

    def session(self) -> Session:
        return self._Session()

    # ---- Social events -----------------------------------------------------

    def add_events(self, events: Iterable[SocialEvent]) -> int:
        """Insert events, skipping duplicates on (source, external_id).

        Uses a portable upsert-ignore for SQLite; falls back to per-row
        insert-with-rollback for other backends.
        """
        events = list(events)
        if not events:
            return 0
        inserted = 0
        with self.session() as s:
            if self.engine.dialect.name == "sqlite":
                for e in events:
                    # Core insert bypasses ORM column defaults, so coalesce here.
                    stmt = (
                        sqlite_insert(SocialEvent)
                        .values(
                            source=e.source,
                            external_id=e.external_id,
                            ticker=e.ticker,
                            author=e.author or "",
                            text=e.text or "",
                            url=e.url or "",
                            weight=e.weight if e.weight is not None else 1.0,
                            sentiment=e.sentiment if e.sentiment is not None else 0.0,
                            author_followers=e.author_followers or 0,
                            text_hash=e.text_hash or "",
                            created_at=e.created_at,
                            ingested_at=e.ingested_at or utcnow(),
                        )
                        .on_conflict_do_nothing(index_elements=["source", "external_id"])
                    )
                    res = s.execute(stmt)
                    inserted += res.rowcount or 0
                s.commit()
            else:
                for e in events:
                    try:
                        with s.begin_nested():
                            s.add(e)
                        inserted += 1
                    except Exception:
                        s.rollback()
                s.commit()
        return inserted

    def mention_stats_since(self, ticker: str, since: datetime) -> MentionStats:
        """Mention counts, distinct sources/authors, and sentiment split."""
        with self.session() as s:
            scope = (SocialEvent.ticker == ticker, SocialEvent.created_at >= since)

            def _count(*extra) -> int:
                return int(
                    s.scalar(select(func.count()).select_from(SocialEvent).where(*scope, *extra))
                    or 0
                )

            mentions = _count()
            sources = int(
                s.scalar(
                    select(func.count(func.distinct(SocialEvent.source))).where(*scope)
                )
                or 0
            )
            authors = int(
                s.scalar(
                    select(func.count(func.distinct(SocialEvent.author))).where(
                        *scope, SocialEvent.author != ""
                    )
                )
                or 0
            )
            weighted = float(
                s.scalar(
                    select(func.coalesce(func.sum(SocialEvent.weight), 0.0)).where(*scope)
                )
                or 0.0
            )
            bullish = _count(SocialEvent.sentiment > 0)
            bearish = _count(SocialEvent.sentiment < 0)

        return MentionStats(
            mentions=mentions,
            sources=sources,
            authors=authors,
            weighted=weighted,
            bullish=bullish,
            bearish=bearish,
        )

    def count_mentions_since(self, ticker: str, since: datetime) -> tuple[int, int, float]:
        """Return (mentions, distinct_sources, weighted_mentions) for a ticker."""
        stats = self.mention_stats_since(ticker, since)
        return stats.mentions, stats.sources, stats.weighted

    def _weighted_between(self, s: Session, ticker: str, start: datetime, end: datetime) -> float:
        return float(
            s.scalar(
                select(func.coalesce(func.sum(SocialEvent.weight), 0.0)).where(
                    SocialEvent.ticker == ticker,
                    SocialEvent.created_at >= start,
                    SocialEvent.created_at < end,
                )
            )
            or 0.0
        )

    def mentions_per_bucket(self, ticker: str, bucket_minutes: int, buckets: int) -> list[float]:
        """Weighted mention counts for the last `buckets` windows (oldest first)."""
        now = utcnow()
        with self.session() as s:
            return [
                self._weighted_between(
                    s,
                    ticker,
                    now - timedelta(minutes=bucket_minutes * i),
                    now - timedelta(minutes=bucket_minutes * (i - 1)),
                )
                for i in range(buckets, 0, -1)
            ]

    def seasonal_buckets(self, ticker: str, bucket_minutes: int, days: int) -> list[float]:
        """Weighted mentions in the same clock window on each of the last `days`.

        Crypto Twitter has a strong daily cycle, so comparing the current bucket
        against the trailing few hours mistakes the normal US-morning ramp for a
        spike. Comparing against the same hour on previous days removes it.
        """
        now = utcnow()
        with self.session() as s:
            return [
                self._weighted_between(
                    s,
                    ticker,
                    now - timedelta(days=k) - timedelta(minutes=bucket_minutes),
                    now - timedelta(days=k),
                )
                for k in range(days, 0, -1)
            ]

    def history_span_hours(self, ticker: str | None = None) -> float:
        """Hours between the oldest stored event and now (0.0 when empty)."""
        with self.session() as s:
            stmt = select(func.min(SocialEvent.created_at))
            if ticker is not None:
                stmt = stmt.where(SocialEvent.ticker == ticker)
            earliest = s.scalar(stmt)
        if earliest is None:
            return 0.0
        if earliest.tzinfo is None:
            earliest = earliest.replace(tzinfo=utcnow().tzinfo)
        return max((utcnow() - earliest).total_seconds() / 3600.0, 0.0)

    # ---- Signals -----------------------------------------------------------

    def add_signal(self, signal: Signal) -> None:
        with self.session() as s:
            s.add(signal)
            s.commit()

    # ---- Trades ------------------------------------------------------------
    #
    # All trade queries accept an optional `strategy` filter. When None, they
    # operate across all strategies (used for global views like the kill switch
    # and total equity); when set, they scope to a single strategy so each
    # methodology enforces its own independent limits and PnL.

    def open_trades(self, strategy: str | None = None) -> Sequence[Trade]:
        with self.session() as s:
            stmt = select(Trade).where(Trade.status == TradeStatus.OPEN)
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return list(s.scalars(stmt))

    def open_trade_for(self, ticker: str, strategy: str | None = None) -> Trade | None:
        with self.session() as s:
            stmt = select(Trade).where(
                Trade.ticker == ticker, Trade.status == TradeStatus.OPEN
            )
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return s.scalar(stmt)

    def closed_trades_for(self, ticker: str, strategy: str | None = None) -> Sequence[Trade]:
        with self.session() as s:
            stmt = select(Trade).where(
                Trade.ticker == ticker, Trade.status == TradeStatus.CLOSED
            )
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return list(s.scalars(stmt.order_by(Trade.closed_at)))

    def closed_trades(self, strategy: str | None = None) -> Sequence[Trade]:
        with self.session() as s:
            stmt = select(Trade).where(Trade.status == TradeStatus.CLOSED)
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return list(s.scalars(stmt.order_by(Trade.closed_at)))

    def count_open_trades(self, strategy: str | None = None) -> int:
        with self.session() as s:
            stmt = select(func.count()).select_from(Trade).where(
                Trade.status == TradeStatus.OPEN
            )
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return int(s.scalar(stmt) or 0)

    def count_trades_since(self, since: datetime, strategy: str | None = None) -> int:
        with self.session() as s:
            stmt = select(func.count()).select_from(Trade).where(Trade.opened_at >= since)
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return int(s.scalar(stmt) or 0)

    def realized_pnl_since(self, since: datetime, strategy: str | None = None) -> float:
        with self.session() as s:
            stmt = select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)).where(
                Trade.closed_at.is_not(None), Trade.closed_at >= since
            )
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return float(s.scalar(stmt) or 0.0)

    def total_realized_pnl(self, strategy: str | None = None) -> float:
        with self.session() as s:
            stmt = select(func.coalesce(func.sum(Trade.realized_pnl), 0.0))
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return float(s.scalar(stmt) or 0.0)

    def last_stop_out_for(self, ticker: str, strategy: str | None = None) -> datetime | None:
        with self.session() as s:
            stmt = select(func.max(Trade.closed_at)).where(
                Trade.ticker == ticker, Trade.exit_reason == ExitReason.STOP_LOSS
            )
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return s.scalar(stmt)

    def add_trade(self, trade: Trade) -> Trade:
        with self.session() as s:
            s.add(trade)
            s.commit()
            s.refresh(trade)
            return trade

    def update_trade(self, trade: Trade) -> None:
        with self.session() as s:
            s.merge(trade)
            s.commit()

    # ---- Reporting ---------------------------------------------------------

    def strategy_stats(self, strategy: str) -> dict:
        """Aggregate performance metrics for one strategy."""
        closed = self.closed_trades(strategy)
        open_count = self.count_open_trades(strategy)
        wins = sum(1 for t in closed if t.realized_pnl > 0)
        total_pnl = sum(t.realized_pnl for t in closed)
        day_pnl = self.realized_pnl_since(utcnow() - timedelta(days=1), strategy)

        holds: list[float] = []
        for t in closed:
            if t.closed_at is not None:
                opened = t.opened_at
                closed_at = t.closed_at
                # Normalize tz for subtraction.
                if opened.tzinfo is None:
                    opened = opened.replace(tzinfo=utcnow().tzinfo)
                if closed_at.tzinfo is None:
                    closed_at = closed_at.replace(tzinfo=utcnow().tzinfo)
                holds.append((closed_at - opened).total_seconds())
        avg_hold_hours = (sum(holds) / len(holds) / 3600.0) if holds else 0.0

        return {
            "strategy": strategy,
            "closed_trades": len(closed),
            "open_positions": open_count,
            "wins": wins,
            "win_rate": (wins / len(closed)) if closed else 0.0,
            "total_pnl": total_pnl,
            "day_pnl": day_pnl,
            "avg_hold_hours": avg_hold_hours,
        }

    # ---- Security audit ----------------------------------------------------

    def add_security_event(self, kind: str, detail: str, severity: str = "INFO") -> None:
        with self.session() as s:
            s.add(SecurityEvent(kind=kind, detail=detail, severity=severity))
            s.commit()
        log.log(
            {"INFO": 20, "WARNING": 30, "CRITICAL": 50}.get(severity, 20),
            "security[%s] %s: %s",
            severity,
            kind,
            detail,
        )
