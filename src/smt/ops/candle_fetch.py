"""Read-only public Coinbase candles for closed trades, written as replay CSVs.

No orders, no API keys, no database writes. Coinbase omits empty bars and does
not offer 4h candles; this module pages the public feed, aggregates complete
UTC buckets when needed, and fills gaps so ``load_candle_csv`` will accept the
file.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..backtest import CSV_FIELDS, _iso
from ..config import get_market
from ..logging_setup import get_logger
from ..market.data import API_BASE, COINBASE_GRANULARITIES
from ..market.indicators import Candle, aggregate_candles
from ..models import Trade
from ..store import Store
from ..trader.exit_policy import ResolvedExitProfile, legacy_profile, resolve_profile
from .reports import GEN8_CONFIG_FINGERPRINT_PREFIX, trades_matching_config_fingerprint

log = get_logger("smt.ops.candle_fetch")

USER_AGENT = "social-momentum-trader"
_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class FetchResult:
    trade_id: int
    ticker: str
    product_id: str
    granularity: int
    bars: int
    filled: int
    path: Path | None
    status: str
    message: str = ""


def _aware(dt: datetime) -> datetime:
    """Treat naive timestamps as UTC; SQLite round-trips drop the zone."""
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _unix(value: datetime | float | int | None) -> int:
    if value is None:
        value = datetime.now(UTC)
    if isinstance(value, datetime):
        return int(_aware(value).timestamp())
    return int(value)


def _floor_ts(ts: int, granularity: int) -> int:
    return (ts // granularity) * granularity


def _ceil_ts(ts: int, granularity: int) -> int:
    return ts if ts % granularity == 0 else _floor_ts(ts, granularity) + granularity


def _profile_for(trade: Trade) -> ResolvedExitProfile:
    if trade.exit_snapshot:
        return resolve_profile(trade.exit_snapshot)
    return legacy_profile(trade.strategy)


def source_granularity(target: int) -> int:
    """Largest Coinbase granularity that divides ``target`` (``target`` itself if listed)."""
    if target in COINBASE_GRANULARITIES:
        return target
    divisors = [gran for gran in COINBASE_GRANULARITIES if target % gran == 0]
    if not divisors:
        raise ValueError(f"no Coinbase granularity divides {target}s")
    return max(divisors)


def trade_candle_window(
    trade: Trade,
    profile: ResolvedExitProfile,
    atr_periods: int,
    *,
    pad_bars: int = 2,
    warmup_extra: int = 5,
    now: datetime | float | int | None = None,
) -> tuple[int, int, int]:
    """Return ``(start_ts, end_ts, granularity)`` as a half-open UTC window.

    Start is the opened bar floored to the trail granularity, then walked back
    ``atr_periods + warmup_extra`` bars so ATR and related indicators can warm
    up. End is the later of the actual close and the hard time-stop, ceiled to
    a bar boundary, plus ``pad_bars``, and capped at the exclusive end of the
    last fully-closed bar at ``now``.
    """
    granularity = profile.trail_granularity_seconds
    if granularity <= 0:
        raise ValueError("trail_granularity_seconds must be positive")
    opened = _unix(trade.opened_at)
    closed = _unix(trade.closed_at or trade.opened_at)
    start = _floor_ts(opened, granularity) - (atr_periods + warmup_extra) * granularity
    hard_stop = opened + int(profile.time_stop_hours) * 3600
    end = _ceil_ts(max(closed, hard_stop), granularity) + pad_bars * granularity
    last_closed_end = _floor_ts(_unix(now), granularity)
    end = min(end, last_closed_end)
    return start, end, granularity


def _parse_rows(rows: object) -> list[Candle]:
    if not isinstance(rows, list):
        raise ValueError("candle payload is not a list")
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        candles.append(
            Candle(
                ts=int(row[0]),
                low=float(row[1]),
                high=float(row[2]),
                open=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    return candles


def _complete_aggregates(
    candles: list[Candle], source_g: int, target_g: int
) -> list[Candle]:
    expected = target_g // source_g
    unique: dict[int, Candle] = {}
    for candle in candles:
        unique[candle.ts] = candle
    source = [unique[ts] for ts in sorted(unique)]
    counts: dict[int, int] = {}
    for candle in source:
        bucket = (candle.ts // target_g) * target_g
        counts[bucket] = counts.get(bucket, 0) + 1
    return [
        candle
        for candle in aggregate_candles(source, target_g)
        if counts.get(candle.ts, 0) == expected
    ]


def _get_page(
    client: httpx.Client,
    product_id: str,
    source_g: int,
    chunk_start: int,
    chunk_end: int,
    sleep: Callable[[float], None],
    pause_s: float,
) -> list[Candle]:
    url = f"{API_BASE}/products/{product_id}/candles"
    # Coinbase treats `end` as inclusive: a span of max_bars * granularity
    # returns max_bars + 1 bars. Request through chunk_end - 1 second.
    params = {
        "granularity": source_g,
        "start": _iso(chunk_start),
        "end": _iso(chunk_end - 1),
    }
    last_error: Exception | None = None
    for attempt in range(_MAX_ATTEMPTS):
        response = client.get(url, params=params)
        status = response.status_code
        if status == 429 or status >= 500:
            last_error = RuntimeError(
                f"HTTP {status} fetching {product_id} {source_g}s "
                f"[{params['start']}, {params['end']}]"
            )
            if attempt + 1 < _MAX_ATTEMPTS:
                sleep(pause_s * (2**attempt))
                continue
            raise last_error
        response.raise_for_status()
        try:
            return _parse_rows(response.json())
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"malformed candle payload for {product_id} {source_g}s"
            ) from exc
    raise last_error or RuntimeError(f"candle fetch failed for {product_id}")


def fetch_candles(
    client: httpx.Client,
    product_id: str,
    granularity: int,
    start_ts: int,
    end_ts: int,
    *,
    max_bars_per_request: int = 300,
    sleep: Callable[[float], None] = time.sleep,
    pause_s: float = 0.2,
) -> list[Candle]:
    """Page public Coinbase candles over ``[start_ts, end_ts)``.

    Chunks are at most ``max_bars_per_request`` bars of the *source* granularity.
    Unsupported target granularities (4h) are built from the largest Coinbase
    granularity that divides them; only complete UTC buckets are kept.
    """
    if granularity <= 0:
        raise ValueError("granularity must be positive")
    if max_bars_per_request <= 0:
        raise ValueError("max_bars_per_request must be positive")
    if end_ts <= start_ts:
        return []

    source_g = source_granularity(granularity)
    max_span = max_bars_per_request * source_g
    collected: list[Candle] = []
    cursor = start_ts
    first = True
    while cursor < end_ts:
        if not first:
            sleep(pause_s)
        first = False
        chunk_end = min(cursor + max_span, end_ts)
        collected.extend(_get_page(client, product_id, source_g, cursor, chunk_end, sleep, pause_s))
        cursor = chunk_end

    unique: dict[int, Candle] = {}
    for candle in collected:
        if candle.ts % source_g:
            continue
        unique[candle.ts] = candle
    source = [unique[ts] for ts in sorted(unique)]
    if granularity != source_g:
        candles = _complete_aggregates(source, source_g, granularity)
    else:
        candles = source
    return [candle for candle in candles if start_ts <= candle.ts < end_ts]


def fill_gaps(candles: Sequence[Candle], granularity: int) -> tuple[list[Candle], int]:
    """Insert flat zero-volume bars so the series is contiguous at ``granularity``."""
    if granularity <= 0:
        raise ValueError("granularity must be positive")
    if not candles:
        return [], 0
    ordered = sorted(candles, key=lambda candle: candle.ts)
    unique: list[Candle] = []
    for candle in ordered:
        if unique and candle.ts == unique[-1].ts:
            unique[-1] = candle
            continue
        unique.append(candle)

    filled: list[Candle] = [unique[0]]
    inserted = 0
    for candle in unique[1:]:
        previous = filled[-1]
        delta = candle.ts - previous.ts
        if delta == granularity:
            filled.append(candle)
            continue
        if delta <= 0 or delta % granularity:
            filled.append(candle)
            continue
        price = previous.close
        missing = delta // granularity - 1
        for step in range(1, missing + 1):
            filled.append(
                Candle(
                    ts=previous.ts + step * granularity,
                    low=price,
                    high=price,
                    open=price,
                    close=price,
                    volume=0.0,
                )
            )
            inserted += 1
        filled.append(candle)
    return filled, inserted


def write_candle_csv(path: Path, candles: Sequence[Candle]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
        writer.writeheader()
        for candle in candles:
            writer.writerow(
                {
                    "timestamp": _iso(candle.ts),
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
            )


def candle_csv_path(out_dir: Path, trade: Trade) -> Path:
    profile = _profile_for(trade)
    return Path(out_dir) / (
        f"trade_{trade.id}_{trade.product_id}_{profile.trail_granularity_seconds}.csv"
    )


def _result(
    trade: Trade,
    granularity: int,
    *,
    bars: int = 0,
    filled: int = 0,
    path: Path | None = None,
    status: str,
    message: str = "",
) -> FetchResult:
    return FetchResult(
        trade_id=int(trade.id or 0),
        ticker=trade.ticker,
        product_id=trade.product_id,
        granularity=granularity,
        bars=bars,
        filled=filled,
        path=path,
        status=status,
        message=message,
    )


def fetch_trade_candles(
    store: Store,
    out_dir: Path,
    *,
    fingerprint_prefix: str = GEN8_CONFIG_FINGERPRINT_PREFIX,
    client: httpx.Client | None = None,
    now: datetime | float | int | None = None,
    overwrite: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    pause_s: float = 0.2,
) -> list[FetchResult]:
    """Fetch public candles for each closed trade matching ``fingerprint_prefix``."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    market = get_market()
    atr_periods = market.atr_periods
    matched = trades_matching_config_fingerprint(store.closed_trades(), fingerprint_prefix)
    owns_client = client is None
    if owns_client:
        client = httpx.Client(
            timeout=market.request_timeout_seconds,
            headers={"User-Agent": USER_AGENT},
        )
    assert client is not None
    results: list[FetchResult] = []
    try:
        for trade in matched:
            results.append(
                _fetch_one_trade(
                    trade,
                    out_dir,
                    client=client,
                    atr_periods=atr_periods,
                    now=now,
                    overwrite=overwrite,
                    sleep=sleep,
                    pause_s=pause_s,
                )
            )
    finally:
        if owns_client:
            client.close()
    return results


def _fetch_one_trade(
    trade: Trade,
    out_dir: Path,
    *,
    client: httpx.Client,
    atr_periods: int,
    now: datetime | float | int | None,
    overwrite: bool,
    sleep: Callable[[float], None],
    pause_s: float,
) -> FetchResult:
    try:
        profile = _profile_for(trade)
        path = candle_csv_path(out_dir, trade)
        if path.exists() and not overwrite:
            return _result(
                trade,
                profile.trail_granularity_seconds,
                path=path,
                status="cached",
            )
        start_ts, end_ts, granularity = trade_candle_window(
            trade, profile, atr_periods, now=now
        )
        candles = fetch_candles(
            client,
            trade.product_id,
            granularity,
            start_ts,
            end_ts,
            sleep=sleep,
            pause_s=pause_s,
        )
        candles, filled = fill_gaps(candles, granularity)
        if len(candles) < 2:
            raise RuntimeError("need at least two candles")
        write_candle_csv(path, candles)
        return _result(
            trade,
            granularity,
            bars=len(candles),
            filled=filled,
            path=path,
            status="ok",
        )
    except Exception as exc:  # noqa: BLE001 - one trade must not stop the batch
        granularity = 0
        path: Path | None = None
        try:
            profile = _profile_for(trade)
            granularity = profile.trail_granularity_seconds
            path = candle_csv_path(out_dir, trade)
        except Exception:  # noqa: BLE001
            pass
        log.warning("candle fetch failed for trade %s: %s", trade.id, exc)
        return _result(
            trade,
            granularity,
            path=path,
            status="error",
            message=str(exc),
        )


def format_fetch_line(result: FetchResult) -> str:
    location = str(result.path) if result.path is not None else "-"
    if result.status == "error":
        return (
            f"error trade={result.trade_id} {result.ticker} {result.product_id} "
            f"g={result.granularity}: {result.message}"
        )
    return (
        f"{result.status} trade={result.trade_id} {result.ticker} {result.product_id} "
        f"g={result.granularity} bars={result.bars} filled={result.filled} -> {location}"
    )
