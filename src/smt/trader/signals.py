"""Signal engine: turn momentum scores into confirmed trade candidates.

Each strategy has its own SignalEngine with its own thresholds, so the same
score stream can produce different candidates per methodology.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import StrategyConfig, UniverseConfig
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
    strategy: str = "intraday"


class SignalEngine:
    """Applies one strategy's entry thresholds + multi-source confirmation."""

    def __init__(self, strategy: StrategyConfig, universe: UniverseConfig):
        self.strategy = strategy
        self.universe = universe

    def candidates(self, scores: list[ScoreResult]) -> list[TradeCandidate]:
        st = self.strategy
        out: list[TradeCandidate] = []
        for s in scores:
            if not self.universe.tradeable(s.ticker):
                continue
            if s.zscore < st.signal_min_zscore:
                continue
            if s.distinct_sources < st.signal_min_distinct_sources:
                continue
            if s.mentions_window < st.signal_min_mentions:
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
                    strategy=st.name,
                )
            )
            log.info(
                "SIGNAL[%s] %s z=%.2f mentions=%d sources=%d",
                st.name,
                s.ticker,
                s.zscore,
                s.mentions_window,
                s.distinct_sources,
            )
        return out
