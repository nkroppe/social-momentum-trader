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


def ema(candles: list[Candle], periods: int) -> float:
    """Standard close EMA seeded by the first full-period SMA."""
    if periods <= 0 or len(candles) < periods:
        return 0.0
    closes = [c.close for c in candles]
    value = mean(closes[:periods])
    alpha = 2.0 / (periods + 1.0)
    for close in closes[periods:]:
        value = alpha * close + (1.0 - alpha) * value
    return value


def rsi(candles: list[Candle], periods: int = 14) -> float:
    """Wilder RSI on closes; returns zero until a full seed window exists."""
    if periods <= 0 or len(candles) < periods + 1:
        return 0.0
    closes = [c.close for c in candles]
    changes = [b - a for a, b in zip(closes[:-1], closes[1:], strict=True)]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]
    avg_gain = mean(gains[:periods])
    avg_loss = mean(losses[:periods])
    for gain, loss in zip(gains[periods:], losses[periods:], strict=True):
        avg_gain = ((periods - 1) * avg_gain + gain) / periods
        avg_loss = ((periods - 1) * avg_loss + loss) / periods
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def rolling_vwap(candles: list[Candle], periods: int) -> float:
    """Volume-weighted typical price over a deterministic rolling window."""
    if periods <= 0 or len(candles) < periods:
        return 0.0
    window = candles[-periods:]
    total_volume = sum(max(c.volume, 0.0) for c in window)
    if total_volume <= 0:
        return 0.0
    notional = sum(((c.high + c.low + c.close) / 3.0) * max(c.volume, 0.0) for c in window)
    return notional / total_volume


def relative_volume(candles: list[Candle], periods: int = 20) -> float:
    """Latest volume divided by the mean of the preceding candles."""
    if periods <= 0 or len(candles) < periods + 1:
        return 0.0
    baseline = [max(c.volume, 0.0) for c in candles[-(periods + 1) : -1]]
    baseline_mean = mean(baseline)
    if baseline_mean <= 0:
        return 0.0
    return max(candles[-1].volume, 0.0) / baseline_mean


def structure_levels(
    candles: list[Candle], periods: int = 20, *, exclude_latest: bool = True
) -> tuple[float, float]:
    """Return the rolling structure high/low, optionally before the trigger bar."""
    end = -1 if exclude_latest else None
    source = candles[:end]
    if periods <= 0 or len(source) < periods:
        return 0.0, 0.0
    window = source[-periods:]
    return max(c.high for c in window), min(c.low for c in window)


def volatility_compression(
    candles: list[Candle],
    lookback: int = 20,
    recent: int = 5,
    max_ratio: float = 0.80,
    *,
    exclude_latest: bool = True,
) -> bool:
    """True when recent true range contracts versus the preceding baseline."""
    source = candles[:-1] if exclude_latest else candles
    if recent <= 0 or lookback <= recent or len(source) < lookback:
        return False
    ranges = _true_ranges(source[-lookback:])
    baseline = mean(ranges[:-recent])
    if baseline <= 0:
        return False
    return mean(ranges[-recent:]) / baseline <= max_ratio


def aggregate_candles(candles: list[Candle], seconds: int) -> list[Candle]:
    """Aggregate lower-timeframe candles into UTC-aligned OHLCV buckets."""
    if seconds <= 0:
        raise ValueError("aggregate seconds must be positive")
    buckets: dict[int, list[Candle]] = {}
    for candle in sorted(candles, key=lambda c: c.ts):
        bucket = (candle.ts // seconds) * seconds
        buckets.setdefault(bucket, []).append(candle)
    return [
        Candle(
            ts=ts,
            open=group[0].open,
            high=max(c.high for c in group),
            low=min(c.low for c in group),
            close=group[-1].close,
            volume=sum(c.volume for c in group),
        )
        for ts, group in sorted(buckets.items())
    ]


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
