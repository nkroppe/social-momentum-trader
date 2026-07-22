"""Momentum scorer: convert mention streams into velocity z-score signals."""

from .velocity import MomentumScorer, ScoreResult

__all__ = ["MomentumScorer", "ScoreResult"]
