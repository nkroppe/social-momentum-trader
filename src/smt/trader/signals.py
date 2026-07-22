"""Signal engine: turn momentum scores into confirmed trade candidates."""

from __future__ import annotations

from dataclasses import dataclass

from ..config import RiskConfig, UniverseConfig
from ..logging_setup import get_logger
from ..scorer import ScoreResult

log = get_logger("smt.signals")


@dataclass
class TradeCandidate:
    ticker: str
    product_id: str
    zscore: float
    mentions: int
    sources: int
    reason: str


class SignalEngine:
    """Applies entry thresholds + multi-source confirmation to scores."""

    def __init__(self, risk: RiskConfig, universe: UniverseConfig):
        self.risk = risk
        self.universe = universe

    def candidates(self, scores: list[ScoreResult]) -> list[TradeCandidate]:
        out: list[TradeCandidate] = []
        for s in scores:
            if not self.universe.tradeable(s.ticker):
                continue
            if s.zscore < self.risk.signal_min_zscore:
                continue
            if s.distinct_sources < self.risk.signal_min_distinct_sources:
                continue
            if s.mentions_window < self.risk.signal_min_mentions:
                continue
            spec = self.universe.symbols[s.ticker]
            out.append(
                TradeCandidate(
                    ticker=s.ticker,
                    product_id=spec.product_id,
                    zscore=s.zscore,
                    mentions=s.mentions_window,
                    sources=s.distinct_sources,
                    reason=s.reason,
                )
            )
            log.info(
                "SIGNAL %s z=%.2f mentions=%d sources=%d",
                s.ticker,
                s.zscore,
                s.mentions_window,
                s.distinct_sources,
            )
        return out
