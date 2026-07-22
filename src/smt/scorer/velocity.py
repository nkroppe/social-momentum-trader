"""Mention-velocity scoring.

For each tradeable ticker we bucket weighted mentions into fixed windows and
compute a z-score of the most recent window vs the trailing baseline. A high
z-score means an unusual burst of social attention = momentum candidate.

Keyword + velocity only (v1). No LLM.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev

from ..config import UniverseConfig
from ..logging_setup import get_logger
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
    reason: str


class MomentumScorer:
    def __init__(
        self,
        store: Store,
        universe: UniverseConfig,
        bucket_minutes: int = 30,
        lookback_buckets: int = 8,
        min_baseline_samples: int = 3,
    ):
        self.store = store
        self.universe = universe
        self.bucket_minutes = bucket_minutes
        self.lookback_buckets = lookback_buckets
        self.min_baseline_samples = min_baseline_samples

    def score_ticker(self, ticker: str) -> ScoreResult:
        buckets = self.store.mentions_per_bucket(ticker, self.bucket_minutes, self.lookback_buckets)
        recent = buckets[-1] if buckets else 0.0
        baseline = buckets[:-1]

        if len(baseline) < self.min_baseline_samples or sum(baseline) == 0:
            # Not enough history to judge; treat as neutral unless there is a
            # cold-start burst (recent activity with no prior baseline).
            z = 0.0
            base_mean = mean(baseline) if baseline else 0.0
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
                f"recent={recent:.1f} vs baseline_mean={base_mean:.1f} "
                f"std={base_std:.2f} -> z={z:.2f}"
            )

        window_start_minutes = self.bucket_minutes * self.lookback_buckets
        from datetime import timedelta

        from ..models import utcnow

        since = utcnow() - timedelta(minutes=window_start_minutes)
        mentions, sources, _ = self.store.count_mentions_since(ticker, since)

        return ScoreResult(
            ticker=ticker,
            zscore=z,
            recent=recent,
            baseline_mean=base_mean,
            mentions_window=mentions,
            distinct_sources=sources,
            reason=reason,
        )

    def score_all(self) -> list[ScoreResult]:
        results = [self.score_ticker(t) for t in self.universe.symbols]
        results.sort(key=lambda r: r.zscore, reverse=True)
        return results
