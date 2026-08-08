"""Mention-velocity scoring.

For each tradeable ticker we bucket weighted mentions into fixed windows and
compute a z-score of the most recent window vs a baseline. A high z-score means
an unusual burst of social attention.

The baseline is seasonal once enough history exists: the current bucket is
compared against the same clock window on previous days rather than against the
trailing few hours. Crypto Twitter has a pronounced daily cycle, so a trailing
baseline reads the normal US-morning ramp as a spike every single day.

Alongside velocity we carry the two quality measures the raw count cannot
express: how many distinct accounts are talking, and whether they are bullish.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from statistics import mean, pstdev

from ..config import UniverseConfig
from ..logging_setup import get_logger
from ..models import utcnow
from ..store import Store

log = get_logger("smt.scorer")


@dataclass
class ScoreResult:
    ticker: str
    zscore: float
    recent: float  # weighted mentions in the most recent bucket
    baseline_mean: float
    mentions_window: int  # raw mentions over the whole lookback
    distinct_sources: int
    distinct_authors: int
    bullish_ratio: float
    directional_posts: int
    baseline_kind: str  # "seasonal" | "trailing" | "none"
    reason: str


class MomentumScorer:
    def __init__(
        self,
        store: Store,
        universe: UniverseConfig,
        bucket_minutes: int = 30,
        lookback_buckets: int = 8,
        min_baseline_samples: int = 3,
        seasonal_days: int = 7,
        seasonal_min_history_hours: int = 48,
    ):
        self.store = store
        self.universe = universe
        self.bucket_minutes = bucket_minutes
        self.lookback_buckets = lookback_buckets
        self.min_baseline_samples = min_baseline_samples
        self.seasonal_days = seasonal_days
        self.seasonal_min_history_hours = seasonal_min_history_hours

    def _baseline(self, ticker: str, recent_excluded: list[float]) -> tuple[list[float], str]:
        """Prefer a same-hour-of-day baseline; fall back to trailing buckets."""
        if self.seasonal_days > 0:
            history_hours = self.store.history_span_hours(ticker)
            if history_hours >= self.seasonal_min_history_hours:
                days = min(self.seasonal_days, int(history_hours // 24))
                seasonal = self.store.seasonal_buckets(ticker, self.bucket_minutes, days)
                if len(seasonal) >= self.min_baseline_samples and sum(seasonal) > 0:
                    return seasonal, "seasonal"
        return recent_excluded, "trailing"

    def score_ticker(self, ticker: str) -> ScoreResult:
        buckets = self.store.mentions_per_bucket(ticker, self.bucket_minutes, self.lookback_buckets)
        recent = buckets[-1] if buckets else 0.0
        baseline, kind = self._baseline(ticker, buckets[:-1])

        window_minutes = self.bucket_minutes * self.lookback_buckets
        since = utcnow() - timedelta(minutes=window_minutes)
        stats = self.store.mention_stats_since(ticker, since)

        if len(baseline) < self.min_baseline_samples or sum(baseline) == 0:
            # Not enough history to judge; stay neutral rather than firing on a
            # cold-start burst that has nothing to be unusual relative to.
            z = 0.0
            base_mean = mean(baseline) if baseline else 0.0
            kind = "none"
            reason = "insufficient baseline"
        else:
            base_mean = mean(baseline)
            # Poisson-style std floor: for count data variance ~= mean, so use
            # sqrt(mean) as a floor. This avoids exploding z when the observed
            # baseline happens to have (near) zero sample variance.
            std_floor = math.sqrt(max(base_mean, 1.0))
            base_std = max(pstdev(baseline), std_floor)
            # Cap to keep a single freak spike from dominating downstream logic.
            z = min((recent - base_mean) / base_std, 50.0)
            reason = (
                f"recent={recent:.1f} vs {kind}_mean={base_mean:.1f} "
                f"std={base_std:.2f} -> z={z:.2f} "
                f"authors={stats.authors} bull={stats.bullish_ratio:.0%}"
                f"({stats.directional} directional)"
            )

        return ScoreResult(
            ticker=ticker,
            zscore=z,
            recent=recent,
            baseline_mean=base_mean,
            mentions_window=stats.mentions,
            distinct_sources=stats.sources,
            distinct_authors=stats.authors,
            bullish_ratio=stats.bullish_ratio,
            directional_posts=stats.directional,
            baseline_kind=kind,
            reason=reason,
        )

    def score_all(self) -> list[ScoreResult]:
        results = [self.score_ticker(t) for t in self.universe.symbols]
        results.sort(key=lambda r: r.zscore, reverse=True)
        return results
