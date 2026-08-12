"""Persistence layer: engine, session, and typed helper queries."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, case, create_engine, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .logging_setup import get_logger
from .market import Candle
from .models import (
    Base,
    ExitReason,
    OpportunityDecision,
    RiskEquitySnapshot,
    SecurityEvent,
    ShadowDecision,
    Signal,
    SocialCount,
    SocialEvent,
    Trade,
    TradeStatus,
    utcnow,
)

log = get_logger("smt.store")

# Columns added after the initial schema; applied idempotently on init_db().
_SOCIAL_EVENT_COLUMNS = (
    ("sentiment", "FLOAT DEFAULT 0.0"),
    ("author_followers", "INTEGER DEFAULT 0"),
    ("author_id", "VARCHAR(128) DEFAULT ''"),
    ("author_following", "INTEGER DEFAULT 0"),
    ("author_posts", "INTEGER DEFAULT 0"),
    ("author_created_at", "TIMESTAMP"),
    ("author_verified", "BOOLEAN DEFAULT FALSE"),
    ("language", "VARCHAR(16) DEFAULT ''"),
    ("possibly_sensitive", "BOOLEAN DEFAULT FALSE"),
    ("is_quote", "BOOLEAN DEFAULT FALSE"),
    ("likes", "INTEGER DEFAULT 0"),
    ("reposts", "INTEGER DEFAULT 0"),
    ("replies", "INTEGER DEFAULT 0"),
    ("quotes", "INTEGER DEFAULT 0"),
    ("bookmarks", "INTEGER DEFAULT 0"),
    ("impressions", "INTEGER DEFAULT 0"),
    ("text_hash", "VARCHAR(32) DEFAULT ''"),
)
_SIGNAL_COLUMNS = (
    ("authors", "INTEGER DEFAULT 0"),
    ("bullish_ratio", "FLOAT DEFAULT 0.0"),
)
_SHADOW_DECISION_COLUMNS = (
    ("trade_id", "INTEGER DEFAULT 0"),
    ("llm_completed_at", "TIMESTAMP"),
    ("opportunity_key", "VARCHAR(64) DEFAULT ''"),
)
_TRADE_COLUMNS = (
    ("original_qty", "FLOAT DEFAULT 0.0"),
    ("highest_price", "FLOAT DEFAULT 0.0"),
    ("initial_risk_per_unit", "FLOAT DEFAULT 0.0"),
    ("partial_taken", "BOOLEAN DEFAULT FALSE"),
    ("partial_realized_pnl", "FLOAT DEFAULT 0.0"),
    ("trailing_stop", "FLOAT DEFAULT 0.0"),
    ("entry_fee_paid", "FLOAT DEFAULT 0.0"),
    ("setup", "VARCHAR(64) DEFAULT ''"),
    ("last_processed_paper_bar_ts", "BIGINT DEFAULT 0"),
)

OPPORTUNITY_LEDGER_VERSION = 1
_OUTCOME_HOURS = (1, 4, 24, 72)


def stable_config_fingerprint(value: str | None = None) -> str:
    """Return the canonical policy hash, or normalize an explicit test identity."""
    if value is None or not value.strip():
        from .policy import trading_policy_identity

        return trading_policy_identity().fingerprint
    normalized = value.strip()
    if len(normalized) == 64 and all(char in "0123456789abcdefABCDEF" for char in normalized):
        return normalized.lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def opportunity_key(
    *,
    config_fingerprint: str,
    run_id: str,
    strategy: str,
    ticker: str,
    trigger_candle_ts: int,
    ledger_version: int = OPPORTUNITY_LEDGER_VERSION,
) -> str:
    stable = (
        f"{ledger_version}|{config_fingerprint}|{run_id}|{strategy}|{ticker}|{trigger_candle_ts}"
    )
    return hashlib.sha256(stable.encode("utf-8")).hexdigest()[:40]


@dataclass(frozen=True)
class MentionStats:
    mentions: int
    sources: int
    authors: int
    weighted: float
    bullish: int
    bearish: int
    engagement: int = 0

    @property
    def directional(self) -> int:
        return self.bullish + self.bearish

    @property
    def bullish_ratio(self) -> float:
        """Share of directional posts that are bullish; 0.0 when none are."""
        return (self.bullish / self.directional) if self.directional else 0.0


@dataclass(frozen=True)
class CountCoverage:
    ticker: str
    expected: int
    observed: int

    @property
    def ratio(self) -> float:
        return self.observed / self.expected if self.expected else 0.0


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
        self._ensure_social_event_dedup_key()
        self._ensure_exit_reason_values()
        self._ensure_columns("social_events", _SOCIAL_EVENT_COLUMNS)
        self._ensure_columns("signals", _SIGNAL_COLUMNS)
        self._ensure_columns("shadow_decisions", _SHADOW_DECISION_COLUMNS)
        self._ensure_columns("trades", _TRADE_COLUMNS)
        self._ensure_shadow_trade_index()
        self._ensure_opportunity_indexes()
        self._backfill_advanced_exit_fields()

    def _ensure_shadow_trade_index(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_shadow_decisions_trade_id "
                    "ON shadow_decisions (trade_id)"
                )
            )

    def _ensure_opportunity_indexes(self) -> None:
        """Install linkage indexes for old and newly-created ledgers."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_shadow_decisions_opportunity_key "
                    "ON shadow_decisions (opportunity_key)"
                )
            )
            conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS uq_opportunity_evaluation "
                    "ON opportunity_decisions "
                    "(ledger_version, config_fingerprint, run_id, strategy, ticker, "
                    "trigger_candle_ts)"
                )
            )

    def _ensure_exit_reason_values(self) -> None:
        """Extend the existing PostgreSQL enum before new exits can persist."""
        if self.engine.dialect.name != "postgresql":
            return
        with self.engine.begin() as conn:
            for value in ("TRAILING_STOP", "ENTRY_RISK"):
                conn.execute(text(f"ALTER TYPE exitreason ADD VALUE IF NOT EXISTS '{value}'"))

    def _backfill_advanced_exit_fields(self) -> None:
        """Seed advanced-exit state for trades created by older versions."""
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE trades SET "
                    "original_qty = CASE WHEN original_qty IS NULL OR original_qty <= 0 "
                    "THEN qty ELSE original_qty END, "
                    "highest_price = CASE WHEN highest_price IS NULL OR highest_price <= 0 "
                    "THEN entry_price ELSE highest_price END, "
                    "initial_risk_per_unit = CASE "
                    "WHEN initial_risk_per_unit IS NULL OR initial_risk_per_unit <= 0 "
                    "THEN CASE WHEN entry_price > stop_loss "
                    "THEN entry_price - stop_loss ELSE 0 END "
                    "ELSE initial_risk_per_unit END, "
                    "entry_fee_paid = CASE WHEN entry_fee_paid IS NULL OR entry_fee_paid <= 0 "
                    "THEN fees_paid ELSE entry_fee_paid END"
                )
            )

    def _ensure_social_event_dedup_key(self) -> None:
        """Move dedup from (source, external_id) to (source, external_id, ticker).

        A single post can tag multiple symbols; the old key kept only the first
        ticker and, on Postgres, a duplicate in the same batch rolled back the
        whole batch.
        """
        dialect = self.engine.dialect.name
        with self.engine.begin() as conn:
            if dialect == "sqlite":
                index_rows = list(conn.execute(text("PRAGMA index_list(social_events)")))
                unique_columns: list[list[str]] = []
                for row in index_rows:
                    if not row[2]:
                        continue
                    name = str(row[1]).replace('"', '""')
                    unique_columns.append(
                        [
                            str(info[2])
                            for info in conn.execute(text(f'PRAGMA index_info("{name}")'))
                        ]
                    )
                has_old = ["source", "external_id"] in unique_columns
                has_new = ["source", "external_id", "ticker"] in unique_columns
                if has_old:
                    # SQLite table-level UNIQUE constraints become immutable
                    # sqlite_autoindex entries. Rebuild the table to remove it.
                    for row in index_rows:
                        name = str(row[1])
                        if not name.startswith("sqlite_autoindex"):
                            escaped = name.replace('"', '""')
                            conn.execute(text(f'DROP INDEX IF EXISTS "{escaped}"'))
                    conn.execute(text("DROP TABLE IF EXISTS social_events_legacy_dedup"))
                    conn.execute(
                        text("ALTER TABLE social_events RENAME TO social_events_legacy_dedup")
                    )
                    SocialEvent.__table__.create(bind=conn)
                    old_columns = {
                        str(row[1])
                        for row in conn.execute(
                            text("PRAGMA table_info(social_events_legacy_dedup)")
                        )
                    }
                    new_columns = {
                        str(row[1])
                        for row in conn.execute(text("PRAGMA table_info(social_events)"))
                    }
                    shared = [column for column in new_columns if column in old_columns]
                    quoted = ", ".join(f'"{column}"' for column in shared)
                    conn.execute(
                        text(
                            f"INSERT OR IGNORE INTO social_events ({quoted}) "
                            f"SELECT {quoted} FROM social_events_legacy_dedup"
                        )
                    )
                    conn.execute(text("DROP TABLE social_events_legacy_dedup"))
                    log.info("rebuilt social_events dedup key to include ticker (sqlite)")
                    return
                if has_new:
                    return
                conn.execute(
                    text(
                        "CREATE UNIQUE INDEX IF NOT EXISTS uq_source_extid_ticker "
                        "ON social_events (source, external_id, ticker)"
                    )
                )
                log.info("migrated: social_events dedup key now includes ticker (sqlite)")
            elif dialect == "postgresql":
                has_new = conn.execute(
                    text("SELECT 1 FROM pg_constraint WHERE conname = 'uq_source_extid_ticker'")
                ).fetchone()
                if has_new:
                    return
                conn.execute(
                    text("ALTER TABLE social_events DROP CONSTRAINT IF EXISTS uq_source_extid")
                )
                conn.execute(
                    text(
                        "ALTER TABLE social_events ADD CONSTRAINT uq_source_extid_ticker "
                        "UNIQUE (source, external_id, ticker)"
                    )
                )
                log.info("migrated: social_events dedup key now includes ticker (postgres)")

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
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {ddl}"))

    def _ensure_trade_strategy_column(self) -> None:
        dialect = self.engine.dialect.name
        with self.engine.begin() as conn:
            if dialect == "sqlite":
                cols = {row[1] for row in conn.execute(text("PRAGMA table_info(trades)"))}
                if "trades" in Base.metadata.tables and "strategy" not in cols and cols:
                    conn.execute(
                        text(
                            "ALTER TABLE trades ADD COLUMN strategy VARCHAR(16) DEFAULT 'intraday'"
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
        """Insert events, skipping duplicates on (source, external_id, ticker)."""
        events = list(events)
        if not events:
            return 0

        def _values(e: SocialEvent) -> dict:
            return {
                "source": e.source,
                "external_id": e.external_id,
                "ticker": e.ticker,
                "author": e.author or "",
                "text": e.text or "",
                "url": e.url or "",
                "weight": e.weight if e.weight is not None else 1.0,
                "sentiment": e.sentiment if e.sentiment is not None else 0.0,
                "author_followers": e.author_followers or 0,
                "author_id": e.author_id or "",
                "author_following": e.author_following or 0,
                "author_posts": e.author_posts or 0,
                "author_created_at": e.author_created_at,
                "author_verified": bool(e.author_verified),
                "language": e.language or "",
                "possibly_sensitive": bool(e.possibly_sensitive),
                "is_quote": bool(e.is_quote),
                "likes": e.likes or 0,
                "reposts": e.reposts or 0,
                "replies": e.replies or 0,
                "quotes": e.quotes or 0,
                "bookmarks": e.bookmarks or 0,
                "impressions": e.impressions or 0,
                "text_hash": e.text_hash or "",
                "created_at": e.created_at,
                "ingested_at": e.ingested_at or utcnow(),
            }

        inserted = 0
        dialect = self.engine.dialect.name
        with self.session() as s:
            for e in events:
                if dialect == "sqlite":
                    stmt = (
                        sqlite_insert(SocialEvent)
                        .values(**_values(e))
                        .on_conflict_do_nothing(index_elements=["source", "external_id", "ticker"])
                    )
                else:
                    stmt = (
                        pg_insert(SocialEvent)
                        .values(**_values(e))
                        .on_conflict_do_nothing(index_elements=["source", "external_id", "ticker"])
                    )
                res = s.execute(stmt)
                # Psycopg can return -1 when rowcount is unavailable; treat as zero.
                if res.rowcount and res.rowcount > 0:
                    inserted += res.rowcount
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
                s.scalar(select(func.count(func.distinct(SocialEvent.source))).where(*scope)) or 0
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
                s.scalar(select(func.coalesce(func.sum(SocialEvent.weight), 0.0)).where(*scope))
                or 0.0
            )
            bullish = _count(SocialEvent.sentiment > 0)
            bearish = _count(SocialEvent.sentiment < 0)
            engagement = int(
                s.scalar(
                    select(
                        func.coalesce(
                            func.sum(
                                SocialEvent.likes
                                + SocialEvent.reposts
                                + SocialEvent.replies
                                + SocialEvent.quotes
                                + SocialEvent.bookmarks
                            ),
                            0,
                        )
                    ).where(*scope)
                )
                or 0
            )

        return MentionStats(
            mentions=mentions,
            sources=sources,
            authors=authors,
            weighted=weighted,
            bullish=bullish,
            bearish=bearish,
            engagement=engagement,
        )

    def recent_social_events(self, ticker: str, limit: int = 12) -> Sequence[SocialEvent]:
        """Newest bounded social context for the sparse L3 judge."""
        with self.session() as s:
            stmt = (
                select(SocialEvent)
                .where(SocialEvent.ticker == ticker)
                .order_by(SocialEvent.created_at.desc())
                .limit(max(limit, 0))
            )
            return list(s.scalars(stmt))

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

    # ---- Uncensored social counts -----------------------------------------

    def add_social_counts(self, counts: Iterable[SocialCount]) -> int:
        """Insert aligned count observations idempotently."""
        rows = list(counts)
        if not rows:
            return 0
        dialect = self.engine.dialect.name
        inserted = 0
        with self.session() as s:
            for count in rows:
                values = {
                    "source": count.source,
                    "ticker": count.ticker,
                    "query": count.query or "",
                    "tweet_count": max(int(count.tweet_count), 0),
                    "window_start": count.window_start,
                    "window_end": count.window_end,
                    "granularity": count.granularity or "minute",
                    "ingested_at": count.ingested_at or utcnow(),
                }
                insert = sqlite_insert if dialect == "sqlite" else pg_insert
                stmt = (
                    insert(SocialCount)
                    .values(**values)
                    .on_conflict_do_nothing(index_elements=["source", "ticker", "window_end"])
                )
                result = s.execute(stmt)
                if result.rowcount and result.rowcount > 0:
                    inserted += result.rowcount
            s.commit()
        return inserted

    def recent_social_counts(
        self,
        ticker: str,
        limit: int = 48,
        *,
        before: datetime | None = None,
    ) -> Sequence[SocialCount]:
        """Newest count observations, optionally strictly before a window end."""
        with self.session() as s:
            stmt = select(SocialCount).where(SocialCount.ticker == ticker)
            if before is not None:
                stmt = stmt.where(SocialCount.window_end < before)
            stmt = stmt.order_by(SocialCount.window_end.desc()).limit(max(limit, 0))
            return list(s.scalars(stmt))

    def social_count_at(self, source: str, ticker: str, window_end: datetime) -> SocialCount | None:
        """Return the exact aligned observation, if this window is already paid for."""
        with self.session() as s:
            return s.scalar(
                select(SocialCount).where(
                    SocialCount.source == source,
                    SocialCount.ticker == ticker,
                    SocialCount.window_end == window_end,
                )
            )

    def has_social_counts(self, ticker: str) -> bool:
        with self.session() as s:
            return bool(
                s.scalar(
                    select(func.count())
                    .select_from(SocialCount)
                    .where(SocialCount.ticker == ticker)
                )
            )

    @staticmethod
    def _aligned_end(now: datetime, bucket_minutes: int) -> datetime:
        aware = now if now.tzinfo else now.replace(tzinfo=UTC)
        seconds = bucket_minutes * 60
        return datetime.fromtimestamp(int(aware.timestamp()) // seconds * seconds, tz=UTC)

    def count_buckets(
        self,
        ticker: str,
        bucket_minutes: int,
        buckets: int,
        *,
        now: datetime | None = None,
    ) -> list[float | None]:
        """Aggregate counts, marking any bucket with missing 30m coverage as None."""
        end = self._aligned_end(now or utcnow(), bucket_minutes)
        start = end - timedelta(minutes=bucket_minutes * buckets)
        with self.session() as s:
            rows = list(
                s.scalars(
                    select(SocialCount)
                    .where(
                        SocialCount.ticker == ticker,
                        SocialCount.window_end > start,
                        SocialCount.window_end <= end,
                    )
                    .order_by(SocialCount.window_end)
                )
            )
        values: list[float | None] = []
        if bucket_minutes % 30:
            return [None] * buckets
        expected_windows = bucket_minutes // 30
        for index in range(buckets):
            left = start + timedelta(minutes=bucket_minutes * index)
            right = left + timedelta(minutes=bucket_minutes)
            covered = {
                self._aware(row.window_end)
                for row in rows
                if self._aware(row.window_end) > left and self._aware(row.window_end) <= right
            }
            if len(covered) != expected_windows:
                values.append(None)
                continue
            values.append(
                float(
                    sum(
                        row.tweet_count
                        for row in rows
                        if self._aware(row.window_end) > left
                        and self._aware(row.window_end) <= right
                    )
                )
            )
        return values

    def seasonal_count_buckets(
        self,
        ticker: str,
        bucket_minutes: int,
        days: int,
        *,
        now: datetime | None = None,
    ) -> list[float | None]:
        """Prior-day buckets, retaining missing coverage as None."""
        end = self._aligned_end(now or utcnow(), bucket_minutes)
        if bucket_minutes % 30:
            return [None] * days
        expected_windows = bucket_minutes // 30
        values: list[float | None] = []
        with self.session() as s:
            for days_ago in range(days, 0, -1):
                right = end - timedelta(days=days_ago)
                left = right - timedelta(minutes=bucket_minutes)
                rows = list(
                    s.scalars(
                        select(SocialCount).where(
                            SocialCount.ticker == ticker,
                            SocialCount.window_end > left,
                            SocialCount.window_end <= right,
                        )
                    )
                )
                covered = {self._aware(row.window_end) for row in rows}
                values.append(
                    float(sum(row.tweet_count for row in rows))
                    if len(covered) == expected_windows
                    else None
                )
        return values

    def count_history_span_hours(self, ticker: str) -> float:
        with self.session() as s:
            earliest = s.scalar(
                select(func.min(SocialCount.window_end)).where(SocialCount.ticker == ticker)
            )
            latest = s.scalar(
                select(func.max(SocialCount.window_end)).where(SocialCount.ticker == ticker)
            )
        if earliest is None or latest is None:
            return 0.0
        return max((self._aware(latest) - self._aware(earliest)).total_seconds() / 3600.0, 0.0)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    # ---- Signals -----------------------------------------------------------

    def add_signal(self, signal: Signal) -> None:
        with self.session() as s:
            s.add(signal)
            s.commit()

    @staticmethod
    def _bounded_reason(value: object, limit: int = 1_000) -> str:
        """Keep audit reasons useful without allowing unbounded text storage."""
        return str(value or "")[:limit]

    def upsert_opportunity(self, **values) -> OpportunityDecision:
        """Insert one candle evaluation or refresh only its deterministic evidence."""
        values = dict(values)
        for name in (
            "outcome_reason",
            "regime_reason",
            "price_reason",
            "setup_reason",
            "confirmation_reason",
            "social_reason",
        ):
            if name in values:
                values[name] = self._bounded_reason(values[name])
        values["updated_at"] = utcnow()
        dialect = self.engine.dialect.name
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        stmt = insert(OpportunityDecision).values(**values)
        evaluation_fields = {
            "product_id",
            "tier",
            "trigger_granularity_seconds",
            "trigger_closed_at",
            "outcome_status",
            "outcome_reason",
            "regime_status",
            "regime_reason",
            "price_status",
            "price_reason",
            "setup_status",
            "setup_name",
            "setup_reason",
            "confirmation_status",
            "confirmation_reason",
            "social_status",
            "social_reason",
            "feature_snapshot",
            "proposed_entry_price",
            "proposed_stop_price",
            "updated_at",
        }
        update_values = {
            name: getattr(stmt.excluded, name)
            for name in evaluation_fields
            if name in values
            and not (
                name in {"proposed_entry_price", "proposed_stop_price"} and values[name] is None
            )
        }
        stmt = stmt.on_conflict_do_update(
            index_elements=[
                "ledger_version",
                "config_fingerprint",
                "run_id",
                "strategy",
                "ticker",
                "trigger_candle_ts",
            ],
            set_=update_values,
        )
        with self.session() as s:
            s.execute(stmt)
            s.commit()
            row = s.scalar(
                select(OpportunityDecision).where(
                    OpportunityDecision.ledger_version == values["ledger_version"],
                    OpportunityDecision.config_fingerprint == values["config_fingerprint"],
                    OpportunityDecision.run_id == values["run_id"],
                    OpportunityDecision.strategy == values["strategy"],
                    OpportunityDecision.ticker == values["ticker"],
                    OpportunityDecision.trigger_candle_ts == values["trigger_candle_ts"],
                )
            )
            if row is None:
                raise RuntimeError("opportunity upsert did not produce a row")
            return row

    def opportunity(self, key: str) -> OpportunityDecision | None:
        with self.session() as s:
            return s.scalar(
                select(OpportunityDecision).where(OpportunityDecision.opportunity_key == key)
            )

    def opportunities(self) -> Sequence[OpportunityDecision]:
        with self.session() as s:
            return list(
                s.scalars(
                    select(OpportunityDecision).order_by(
                        OpportunityDecision.evaluated_at,
                        OpportunityDecision.id,
                    )
                )
            )

    def enrich_opportunity(self, key: str, **values) -> bool:
        """Monotonically add downstream LLM, risk, execution, and linkage facts."""
        allowed = {
            "llm_status",
            "llm_score",
            "llm_veto",
            "llm_reason",
            "risk_status",
            "risk_reason",
            "execution_status",
            "execution_reason",
            "proposed_entry_price",
            "proposed_stop_price",
            "proposed_notional_usd",
            "proposed_risk_usd",
            "portfolio_equity",
            "portfolio_existing_heat",
            "portfolio_proposed_heat",
            "portfolio_gross_exposure",
            "portfolio_symbol_exposure",
            "portfolio_micro_exposure",
            "shadow_decision_key",
            "trade_id",
        }
        updates = {name: value for name, value in values.items() if name in allowed}
        for name in (
            "llm_reason",
            "risk_reason",
            "execution_reason",
        ):
            if name in updates:
                updates[name] = self._bounded_reason(updates[name])
        if not updates:
            return False
        updates["updated_at"] = utcnow()
        with self.session() as s:
            result = s.execute(
                update(OpportunityDecision)
                .where(OpportunityDecision.opportunity_key == key)
                .values(**updates)
            )
            s.commit()
            return bool(result.rowcount and result.rowcount > 0)

    def pending_opportunity_maturations(self) -> Sequence[OpportunityDecision]:
        """Rows with at least one prospective horizon still unlabeled."""
        with self.session() as s:
            return list(
                s.scalars(
                    select(OpportunityDecision).where(
                        (OpportunityDecision.outcome_1h_at.is_(None))
                        | (OpportunityDecision.outcome_4h_at.is_(None))
                        | (OpportunityDecision.outcome_24h_at.is_(None))
                        | (OpportunityDecision.outcome_72h_at.is_(None))
                    )
                )
            )

    def mature_opportunity(
        self,
        key: str,
        candles: Sequence[Candle],
        *,
        as_of: datetime | None = None,
    ) -> bool:
        """Label due horizons from candles that begin strictly after evaluation."""
        row = self.opportunity(key)
        if row is None:
            return False
        now = self._aware(as_of or utcnow())
        evaluated = self._aware(row.evaluated_at)
        granularity = int(row.trigger_granularity_seconds)
        if granularity <= 0:
            return False
        first_post_evaluation_ts = (int(evaluated.timestamp()) // granularity + 1) * granularity
        reference = float(row.proposed_entry_price or 0.0)
        if reference <= 0:
            reference = float((row.feature_snapshot or {}).get("trigger_close", 0.0))
        if reference <= 0:
            return False

        updates: dict[str, object] = {}
        for hours in _OUTCOME_HOURS:
            at_name = f"outcome_{hours}h_at"
            if getattr(row, at_name) is not None:
                continue
            target = evaluated + timedelta(hours=hours)
            if now < target:
                continue
            eligible = [
                candle
                for candle in candles
                if int(candle.ts) >= first_post_evaluation_ts
                and int(candle.ts) + granularity <= int(target.timestamp())
            ]
            if not eligible:
                continue
            last = max(eligible, key=lambda candle: int(candle.ts))
            updates[f"return_{hours}h"] = float(last.close) / reference - 1.0
            updates[f"mae_{hours}h"] = (
                min(float(candle.low) for candle in eligible) / reference - 1.0
            )
            updates[f"mfe_{hours}h"] = (
                max(float(candle.high) for candle in eligible) / reference - 1.0
            )
            updates[at_name] = now
        if not updates:
            return False
        updates["updated_at"] = utcnow()
        with self.session() as s:
            result = s.execute(
                update(OpportunityDecision)
                .where(OpportunityDecision.opportunity_key == key)
                .values(**updates)
            )
            s.commit()
            return bool(result.rowcount and result.rowcount > 0)

    def upsert_shadow_decision(self, **values) -> None:
        """Create or refresh one stable setup audit without duplicate spam."""
        values = {**values, "updated_at": utcnow()}
        dialect = self.engine.dialect.name
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        stmt = insert(ShadowDecision).values(**values)
        update_values = {
            key: getattr(stmt.excluded, key)
            for key in values
            if key not in {"decision_key", "first_evaluated_at"}
            and not (key == "trade_id" and not int(values.get("trade_id") or 0))
            and not (key == "opportunity_key" and not str(values.get("opportunity_key") or ""))
        }
        preserve_approved = and_(
            func.coalesce(ShadowDecision.trade_id, 0) > 0,
            ShadowDecision.risk_status == "approved",
        )
        if "risk_status" in update_values:
            update_values["risk_status"] = case(
                (preserve_approved, ShadowDecision.risk_status),
                else_=stmt.excluded.risk_status,
            )
        if "risk_reason" in update_values:
            update_values["risk_reason"] = case(
                (preserve_approved, ShadowDecision.risk_reason),
                else_=stmt.excluded.risk_reason,
            )
        stmt = stmt.on_conflict_do_update(
            index_elements=["decision_key"],
            set_=update_values,
        )
        with self.session() as s:
            s.execute(stmt)
            s.commit()

    def shadow_decision(self, decision_key: str) -> ShadowDecision | None:
        with self.session() as s:
            return s.scalar(
                select(ShadowDecision).where(ShadowDecision.decision_key == decision_key)
            )

    def shadow_decisions_between(self, start: datetime, end: datetime) -> Sequence[ShadowDecision]:
        """Audited setups first observed in the half-open UTC window."""
        with self.session() as s:
            return list(
                s.scalars(
                    select(ShadowDecision)
                    .where(
                        ShadowDecision.first_evaluated_at >= start,
                        ShadowDecision.first_evaluated_at < end,
                    )
                    .order_by(ShadowDecision.first_evaluated_at, ShadowDecision.id)
                )
            )

    def trades_by_ids(self, trade_ids: Iterable[int]) -> dict[int, Trade]:
        """Load linked trades in one indexed query."""
        ids = {int(value) for value in trade_ids if int(value) > 0}
        if not ids:
            return {}
        with self.session() as s:
            rows = list(s.scalars(select(Trade).where(Trade.id.in_(ids))))
        return {trade.id: trade for trade in rows}

    def link_shadow_trade(self, decision_key: str, trade_id: int) -> bool:
        """Set the first nonzero trade link and never overwrite it later."""
        if trade_id <= 0:
            return False
        with self.session() as s:
            opportunity = s.scalar(
                select(ShadowDecision.opportunity_key).where(
                    ShadowDecision.decision_key == decision_key
                )
            )
            result = s.execute(
                update(ShadowDecision)
                .where(
                    ShadowDecision.decision_key == decision_key,
                    (ShadowDecision.trade_id.is_(None)) | (ShadowDecision.trade_id == 0),
                )
                .values(trade_id=trade_id, updated_at=utcnow())
            )
            s.commit()
            linked = bool(result.rowcount and result.rowcount > 0)
        if opportunity:
            self.enrich_opportunity(str(opportunity), trade_id=trade_id)
        return linked

    def count_coverage(
        self,
        ticker: str,
        start: datetime,
        end: datetime,
        window_minutes: int,
        *,
        source: str = "x",
    ) -> CountCoverage:
        """Exact distinct aligned windows observed versus expected."""
        if window_minutes <= 0 or end <= start:
            return CountCoverage(ticker, 0, 0)
        seconds = window_minutes * 60
        first_epoch = (int(self._aware(start).timestamp()) // seconds + 1) * seconds
        last_epoch = int(self._aware(end).timestamp()) // seconds * seconds
        expected_ends = {
            datetime.fromtimestamp(epoch, tz=UTC)
            for epoch in range(first_epoch, last_epoch + 1, seconds)
        }
        with self.session() as s:
            rows = list(
                s.scalars(
                    select(SocialCount.window_end).where(
                        SocialCount.source == source,
                        SocialCount.ticker == ticker,
                        SocialCount.window_end > start,
                        SocialCount.window_end <= end,
                    )
                )
            )
        observed = {self._aware(value) for value in rows} & expected_ends
        return CountCoverage(ticker, len(expected_ends), len(observed))

    def count_observation_days(self, tickers: Iterable[str], start: datetime, end: datetime) -> int:
        names = set(tickers)
        if not names:
            return 0
        with self.session() as s:
            rows = list(
                s.scalars(
                    select(SocialCount.window_end).where(
                        SocialCount.source == "x",
                        SocialCount.ticker.in_(names),
                        SocialCount.window_end > start,
                        SocialCount.window_end <= end,
                    )
                )
            )
        return len({self._aware(value).date() for value in rows})

    def update_shadow_llm(
        self,
        decision_key: str,
        *,
        status: str,
        score: float,
        veto: bool,
        reason: str,
    ) -> bool:
        """Update only asynchronous LLM fields on an existing setup audit."""
        with self.session() as s:
            opportunity = s.scalar(
                select(ShadowDecision.opportunity_key).where(
                    ShadowDecision.decision_key == decision_key
                )
            )
            result = s.execute(
                update(ShadowDecision)
                .where(ShadowDecision.decision_key == decision_key)
                .values(
                    llm_status=status,
                    llm_score=score,
                    llm_veto=veto,
                    llm_reason=reason,
                    llm_completed_at=utcnow(),
                    updated_at=utcnow(),
                )
            )
            s.commit()
            updated = bool(result.rowcount and result.rowcount > 0)
        if opportunity:
            self.enrich_opportunity(
                str(opportunity),
                llm_status=status,
                llm_score=score,
                llm_veto=veto,
                llm_reason=reason,
            )
        return updated

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
            stmt = select(Trade).where(Trade.ticker == ticker, Trade.status == TradeStatus.OPEN)
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return s.scalar(stmt)

    def closed_trades_for(self, ticker: str, strategy: str | None = None) -> Sequence[Trade]:
        with self.session() as s:
            stmt = select(Trade).where(Trade.ticker == ticker, Trade.status == TradeStatus.CLOSED)
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return list(s.scalars(stmt.order_by(Trade.closed_at)))

    def closed_trades(self, strategy: str | None = None) -> Sequence[Trade]:
        with self.session() as s:
            stmt = select(Trade).where(Trade.status == TradeStatus.CLOSED)
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return list(s.scalars(stmt.order_by(Trade.closed_at)))

    def closed_trades_between(
        self, start: datetime, end: datetime, strategy: str | None = None
    ) -> Sequence[Trade]:
        """Trades closed within [start, end), oldest first."""
        with self.session() as s:
            stmt = select(Trade).where(
                Trade.status == TradeStatus.CLOSED,
                Trade.closed_at.is_not(None),
                Trade.closed_at >= start,
                Trade.closed_at < end,
            )
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return list(s.scalars(stmt.order_by(Trade.closed_at)))

    def count_trades_opened_between(
        self, start: datetime, end: datetime, strategy: str | None = None
    ) -> int:
        with self.session() as s:
            stmt = (
                select(func.count())
                .select_from(Trade)
                .where(Trade.opened_at >= start, Trade.opened_at < end)
            )
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return int(s.scalar(stmt) or 0)

    def count_open_trades(self, strategy: str | None = None) -> int:
        with self.session() as s:
            stmt = select(func.count()).select_from(Trade).where(Trade.status == TradeStatus.OPEN)
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

    def closed_trades_filtered(
        self,
        *,
        strategy: str | None = None,
        ticker: str | None = None,
        exit_reason: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[Trade], int]:
        """Paginated closed-trade history for the dashboard."""
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        with self.session() as s:
            stmt = select(Trade).where(Trade.status == TradeStatus.CLOSED)
            count_stmt = (
                select(func.count()).select_from(Trade).where(Trade.status == TradeStatus.CLOSED)
            )
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
                count_stmt = count_stmt.where(Trade.strategy == strategy)
            if ticker is not None:
                stmt = stmt.where(Trade.ticker == ticker)
                count_stmt = count_stmt.where(Trade.ticker == ticker)
            if exit_reason is not None:
                stmt = stmt.where(Trade.exit_reason == ExitReason(exit_reason))
                count_stmt = count_stmt.where(Trade.exit_reason == ExitReason(exit_reason))
            if start is not None:
                stmt = stmt.where(Trade.closed_at.is_not(None), Trade.closed_at >= start)
                count_stmt = count_stmt.where(
                    Trade.closed_at.is_not(None), Trade.closed_at >= start
                )
            if end is not None:
                stmt = stmt.where(Trade.closed_at.is_not(None), Trade.closed_at < end)
                count_stmt = count_stmt.where(Trade.closed_at.is_not(None), Trade.closed_at < end)
            total = int(s.scalar(count_stmt) or 0)
            rows = list(
                s.scalars(
                    stmt.order_by(Trade.closed_at.desc(), Trade.id.desc())
                    .limit(limit)
                    .offset(offset)
                )
            )
            return rows, total

    def total_fees_paid(self, strategy: str | None = None) -> float:
        with self.session() as s:
            stmt = select(func.coalesce(func.sum(Trade.fees_paid), 0.0))
            if strategy is not None:
                stmt = stmt.where(Trade.strategy == strategy)
            return float(s.scalar(stmt) or 0.0)

    def recent_opportunities(self, limit: int = 50) -> Sequence[OpportunityDecision]:
        limit = max(1, min(limit, 200))
        with self.session() as s:
            return list(
                s.scalars(
                    select(OpportunityDecision)
                    .order_by(
                        OpportunityDecision.evaluated_at.desc(), OpportunityDecision.id.desc()
                    )
                    .limit(limit)
                )
            )

    def opportunity_status_counts(self) -> dict[str, int]:
        with self.session() as s:
            rows = s.execute(
                select(OpportunityDecision.outcome_status, func.count()).group_by(
                    OpportunityDecision.outcome_status
                )
            )
            return {str(status): int(count) for status, count in rows}

    def recent_shadow_decisions(self, limit: int = 50) -> Sequence[ShadowDecision]:
        limit = max(1, min(limit, 200))
        with self.session() as s:
            return list(
                s.scalars(
                    select(ShadowDecision)
                    .order_by(ShadowDecision.updated_at.desc(), ShadowDecision.id.desc())
                    .limit(limit)
                )
            )

    def shadow_summary(self) -> tuple[int, int, dict[str, int]]:
        """Full-table audit totals, independent of the recent-row page."""
        with self.session() as s:
            total = int(s.scalar(select(func.count()).select_from(ShadowDecision)) or 0)
            vetoes = int(
                s.scalar(
                    select(func.count())
                    .select_from(ShadowDecision)
                    .where(ShadowDecision.llm_veto.is_(True))
                )
                or 0
            )
            rows = s.execute(
                select(ShadowDecision.social_decision, func.count()).group_by(
                    ShadowDecision.social_decision
                )
            )
            social = {(str(status) if status else "unknown"): int(count) for status, count in rows}
            return total, vetoes, social

    def list_risk_equity_snapshots(self, limit: int = 60) -> Sequence[RiskEquitySnapshot]:
        """Read-only halt baselines; does not create missing buckets."""
        limit = max(1, min(limit, 200))
        with self.session() as s:
            return list(
                s.scalars(
                    select(RiskEquitySnapshot)
                    .order_by(RiskEquitySnapshot.bucket_start.desc(), RiskEquitySnapshot.id.desc())
                    .limit(limit)
                )
            )

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

    def risk_equity_baseline(
        self,
        strategy: str,
        period: str,
        bucket_start: datetime,
        current_equity: float,
    ) -> float:
        """Return the persisted first observed equity for this UTC period."""
        with self.session() as s:
            stmt = select(RiskEquitySnapshot).where(
                RiskEquitySnapshot.strategy == strategy,
                RiskEquitySnapshot.period == period,
                RiskEquitySnapshot.bucket_start == bucket_start,
            )
            existing = s.scalar(stmt)
            if existing is not None:
                return existing.equity
            snapshot = RiskEquitySnapshot(
                strategy=strategy,
                period=period,
                bucket_start=bucket_start,
                equity=current_equity,
            )
            s.add(snapshot)
            s.commit()
            return current_equity
