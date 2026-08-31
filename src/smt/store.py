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
    ("config_fingerprint", "VARCHAR(64) DEFAULT ''"),
    ("exit_profile_label", "VARCHAR(64) DEFAULT ''"),
    ("exit_snapshot", "JSON"),
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
        self._ensure_trade_snapshot_index()
        self._ensure_shadow_trade_index()
        self._ensure_opportunity_indexes()
        self._backfill_advanced_exit_fields()

    def _ensure_trade_snapshot_index(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_trades_config_fingerprint "
                    "ON trades (config_fingerprint)"
                )
            )

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
            for value in ("TRAILING_STOP", "ENTRY_RISK", "STALE_TIME_STOP"):
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
