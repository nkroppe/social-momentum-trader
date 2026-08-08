"""Deterministic seeding helpers for the paper demo and tests.

Creates a baseline of steady chatter plus a multi-source burst so that BOTH
the intraday and swing strategies cross their entry thresholds on the same
ticker, without external credentials or waiting for real time to pass.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from .config import StrategyConfig
from .models import SocialEvent, utcnow
from .store import Store

# Distinct sources so the strictest strategy's source-confirmation
# requirement (>= 2) is satisfied.
_BURST_SOURCES = ("reddit", "x", "mock")


def seed_momentum(
    store: Store,
    ticker: str,
    strategies: list[StrategyConfig],
    burst_per_source: int = 10,
) -> None:
    """Seed baseline + burst sized to trigger every provided strategy."""
    max_bucket = max(st.scorer_bucket_minutes for st in strategies)
    max_lookback = max(st.scorer_lookback_buckets for st in strategies)

    now = utcnow()

    # Steady baseline: one neutral mention per older bucket across the widest
    # window. Neutral so it does not dilute the burst's bullish ratio.
    baseline: list[SocialEvent] = []
    for i in range(max_lookback, 1, -1):
        ts = now - timedelta(minutes=max_bucket * i - 1)
        baseline.append(
            SocialEvent(
                source="reddit",
                external_id=uuid.uuid4().hex,
                ticker=ticker,
                author=f"baseline{i}",
                text=f"${ticker} chatter",
                url="",
                weight=1.0,
                sentiment=0.0,
                created_at=ts,
            )
        )
    store.add_events(baseline)

    # Burst now across three distinct sources. Each post gets its own author and
    # bullish polarity so the distinct-author and sentiment gates are satisfied.
    burst: list[SocialEvent] = []
    for src in _BURST_SOURCES:
        for n in range(burst_per_source):
            burst.append(
                SocialEvent(
                    source=src,
                    external_id=uuid.uuid4().hex,
                    ticker=ticker,
                    author=f"{src}user{n}",
                    text=f"${ticker} breaking out, bullish momentum",
                    url="",
                    weight=1.0,
                    sentiment=1.0,
                    created_at=now,
                )
            )
    store.add_events(burst)
