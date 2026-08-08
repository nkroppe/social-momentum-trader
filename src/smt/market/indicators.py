"""Price/volume indicators for confirmation, sizing, and volatility-scaled exits.

All functions take candles ordered oldest-first. They degrade gracefully when
history is short by returning 0.0 rather than raising; callers treat a zero
result as "no confirmation available" and fail closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean, pstdev


@dataclass(frozen=True)
class Candle:
    ts: int
    low: float
    high: float
    open: float
    close: float
    volume: float


def _true_ranges(candles: list[Candle]) -> list[float]:
    trs: list[float] = []
    prev_close: float | None = None
    for c in candles:
        if prev_close is None:
            trs.append(max(c.high - c.low, 0.0))
        else:
            trs.append(max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close)))
        prev_close = c.close
    return trs


def atr(candles: list[Candle], periods: int = 14) -> float:
    """Average true range over the last `periods` candles, in price units."""
    if len(candles) < 2:
        return 0.0
    trs = _true_ranges(candles)[-periods:]
    return mean(trs) if trs else 0.0


def sma(candles: list[Candle], periods: int) -> float:
    closes = [c.close for c in candles][-periods:]
    return mean(closes) if closes else 0.0


def trailing_return(candles: list[Candle], periods: int) -> float:
    """Fractional close-to-close return over the last `periods` candles."""
    if len(candles) < 2:
        return 0.0
    span = min(periods, len(candles) - 1)
    start = candles[-1 - span].close
    if start <= 0:
        return 0.0
    return (candles[-1].close - start) / start


def horizon_volatility(atr_pct: float, hold_hours: float, candle_seconds: int) -> float:
    """Scale a per-candle ATR to an expected move over `hold_hours`.

    Volatility grows with the square root of time, so a 1-hour ATR badly
    understates how far a 48-hour hold can travel. Exit levels and position
    sizing both work off this so they stay on the same scale.
    """
    candle_hours = max(candle_seconds / 3600.0, 1e-9)
    periods = max(hold_hours / candle_hours, 1.0)
    return atr_pct * math.sqrt(periods)


def volume_zscore(candles: list[Candle], periods: int = 24) -> float:
    """z-score of the latest candle's volume vs the preceding `periods`."""
    if len(candles) < 4:
        return 0.0
    vols = [c.volume for c in candles]
    recent = vols[-1]
    baseline = vols[-(periods + 1) : -1]
    if len(baseline) < 2:
        return 0.0
    base_mean = mean(baseline)
    if base_mean <= 0:
        return 0.0
    # Relative floor: a near-flat baseline should not produce an explosive z.
    base_std = max(pstdev(baseline), 0.1 * base_mean)
    return (recent - base_mean) / base_std
