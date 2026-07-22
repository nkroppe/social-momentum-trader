"""Persistence layer: engine, session, and typed helper queries."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .logging_setup import get_logger
from .models import Base, SecurityEvent, Signal, SocialEvent, Trade, TradeStatus, utcnow

log = get_logger("smt.store")


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
        log.info("Database ready at %s", self.database_url)

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

    def count_mentions_since(self, ticker: str, since: datetime) -> tuple[int, int, float]:
        """Return (mentions, distinct_sources, weighted_mentions) for a ticker."""
        with self.session() as s:
            mentions = (
                s.scalar(
                    select(func.count())
                    .select_from(SocialEvent)
                    .where(SocialEvent.ticker == ticker, SocialEvent.created_at >= since)
                )
                or 0
            )
            sources = (
                s.scalar(
                    select(func.count(func.distinct(SocialEvent.source))).where(
                        SocialEvent.ticker == ticker, SocialEvent.created_at >= since
                    )
                )
                or 0
            )
            weighted = (
                s.scalar(
                    select(func.coalesce(func.sum(SocialEvent.weight), 0.0)).where(
                        SocialEvent.ticker == ticker, SocialEvent.created_at >= since
                    )
                )
                or 0.0
            )
        return int(mentions), int(sources), float(weighted)

    def mentions_per_bucket(self, ticker: str, bucket_minutes: int, buckets: int) -> list[float]:
        """Weighted mention counts for the last `buckets` windows (oldest first)."""
        now = utcnow()
        result: list[float] = []
        with self.session() as s:
            for i in range(buckets, 0, -1):
                start = now - timedelta(minutes=bucket_minutes * i)
                end = now - timedelta(minutes=bucket_minutes * (i - 1))
                val = (
                    s.scalar(
                        select(func.coalesce(func.sum(SocialEvent.weight), 0.0)).where(
                            SocialEvent.ticker == ticker,
                            SocialEvent.created_at >= start,
                            SocialEvent.created_at < end,
                        )
                    )
                    or 0.0
                )
                result.append(float(val))
        return result

    # ---- Signals -----------------------------------------------------------

    def add_signal(self, signal: Signal) -> None:
        with self.session() as s:
            s.add(signal)
            s.commit()

    # ---- Trades ------------------------------------------------------------

    def open_trades(self) -> Sequence[Trade]:
        with self.session() as s:
            return list(s.scalars(select(Trade).where(Trade.status == TradeStatus.OPEN)))

    def open_trade_for(self, ticker: str) -> Trade | None:
        with self.session() as s:
            return s.scalar(
                select(Trade).where(Trade.ticker == ticker, Trade.status == TradeStatus.OPEN)
            )

    def closed_trades_for(self, ticker: str) -> Sequence[Trade]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(Trade)
                    .where(Trade.ticker == ticker, Trade.status == TradeStatus.CLOSED)
                    .order_by(Trade.closed_at)
                )
            )

    def count_open_trades(self) -> int:
        with self.session() as s:
            return int(
                s.scalar(
                    select(func.count()).select_from(Trade).where(Trade.status == TradeStatus.OPEN)
                )
                or 0
            )

    def count_trades_since(self, since: datetime) -> int:
        with self.session() as s:
            return int(
                s.scalar(select(func.count()).select_from(Trade).where(Trade.opened_at >= since))
                or 0
            )

    def realized_pnl_since(self, since: datetime) -> float:
        with self.session() as s:
            return float(
                s.scalar(
                    select(func.coalesce(func.sum(Trade.realized_pnl), 0.0)).where(
                        Trade.closed_at.is_not(None), Trade.closed_at >= since
                    )
                )
                or 0.0
            )

    def total_realized_pnl(self) -> float:
        with self.session() as s:
            return float(s.scalar(select(func.coalesce(func.sum(Trade.realized_pnl), 0.0))) or 0.0)

    def last_stop_out_for(self, ticker: str) -> datetime | None:
        from .models import ExitReason

        with self.session() as s:
            return s.scalar(
                select(func.max(Trade.closed_at)).where(
                    Trade.ticker == ticker, Trade.exit_reason == ExitReason.STOP_LOSS
                )
            )

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
