"""Public Coinbase market data: candles, spot price, and derived technicals.

Uses the unauthenticated Coinbase Exchange market-data API so price
confirmation, ATR-based exits, volatility sizing, and the regime filter behave
identically in paper and live mode without needing API credentials.

Every read is cached; products that 404 (not listed on Coinbase) are negatively
cached so a bad universe entry does not generate a request every loop.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from ..config import MarketConfig
from ..logging_setup import get_logger
from .indicators import Candle, aggregate_candles, atr, sma, trailing_return, volume_zscore

log = get_logger("smt.market")

API_BASE = "https://api.exchange.coinbase.com"
COINBASE_GRANULARITIES = frozenset({60, 300, 900, 3_600, 21_600, 86_400})


class MarketDataUnavailable(RuntimeError):
    """Fresh, internally consistent market data could not be obtained."""


@dataclass(frozen=True)
class TopOfBookQuote:
    """Typed Coinbase level-1 book observed by this process."""

    product_id: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    sequence: int
    observed_at: float

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        return (self.spread / self.midpoint * 10_000.0) if self.midpoint > 0 else float("inf")

    @property
    def bid_notional(self) -> float:
        return self.bid * self.bid_size

    @property
    def ask_notional(self) -> float:
        return self.ask * self.ask_size

    def age_seconds(self, now: float | None = None) -> float:
        return max((time.time() if now is None else now) - self.observed_at, 0.0)


@dataclass
class TechnicalSnapshot:
    """Point-in-time technicals for one product.

    `ok` is False whenever the data needed for a confirmation decision is
    missing, which callers treat as "do not trade" when fail_closed is set.
    """

    product_id: str
    ok: bool
    price: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    trailing_return: float = 0.0
    sma: float = 0.0
    volume_z: float = 0.0
    candles: int = 0
    detail: str = ""

    @property
    def above_sma(self) -> bool:
        return self.sma > 0 and self.price > self.sma


@dataclass
class _Cached:
    at: float
    candles: list[Candle] = field(default_factory=list)


class MarketData:
    def __init__(self, cfg: MarketConfig, client: httpx.Client | None = None):
        self.cfg = cfg
        self._client = client or httpx.Client(
            timeout=cfg.request_timeout_seconds,
            headers={"User-Agent": "social-momentum-trader"},
        )
        self._candles: dict[tuple[str, int], _Cached] = {}
        self._quotes: dict[str, tuple[float, TopOfBookQuote]] = {}
        self._unavailable: dict[str, float] = {}

    # ---- Raw fetches --------------------------------------------------------

    def _is_unavailable(self, product_id: str) -> bool:
        until = self._unavailable.get(product_id)
        if until is None:
            return False
        if time.monotonic() >= until:
            del self._unavailable[product_id]
            return False
        return True

    def _mark_unavailable(self, product_id: str) -> None:
        self._unavailable[product_id] = time.monotonic() + self.cfg.unavailable_retry_seconds

    def _candle_freshness_limit(self, granularity: int) -> float:
        if granularity == self.cfg.paper_bar_granularity_seconds:
            return self.cfg.paper_bar_max_age_seconds
        return granularity * self.cfg.candle_max_age_multiplier

    def validate_candles(
        self,
        candles: list[Candle],
        granularity: int,
        *,
        now: float | None = None,
    ) -> tuple[bool, str]:
        """Validate OHLCV shape, exact continuity, closure, and recency."""
        if not candles:
            return False, "no candles"
        current = time.time() if now is None else now
        previous_ts: int | None = None
        for candle in candles:
            if candle.ts % granularity:
                return False, f"unaligned candle at {candle.ts}"
            if previous_ts is not None and candle.ts != previous_ts + granularity:
                return False, f"candle gap after {previous_ts}"
            if (
                candle.open <= 0
                or candle.close <= 0
                or candle.low <= 0
                or candle.high <= 0
                or candle.volume < 0
                or candle.low > min(candle.open, candle.close)
                or candle.high < max(candle.open, candle.close)
                or candle.low > candle.high
            ):
                return False, f"invalid OHLCV candle at {candle.ts}"
            previous_ts = candle.ts

        latest_end = candles[-1].ts + granularity
        if latest_end > current:
            return False, f"latest candle at {candles[-1].ts} is not closed"
        age = current - latest_end
        limit = self._candle_freshness_limit(granularity)
        if age > limit:
            return False, f"latest candle is {age:.1f}s old (max {limit:.1f}s)"
        return True, "ok"

    def fill_candle_gaps(
        self,
        candles: list[Candle],
        granularity: int,
        *,
        max_fill_bars: int | None = None,
    ) -> tuple[list[Candle], int]:
        """Insert flat zero-volume bars for short Coinbase omissions.

        Thin products often skip empty minutes instead of publishing a
        zero-volume candle. PAPER bar walks need exact continuity, so we
        synthesize missing slots from the prior close — but only up to
        ``paper_bar_gap_fill_max_bars`` consecutive missing minutes. Larger
        holes are left intact so ``validate_candles`` still fails closed.
        """
        limit = (
            self.cfg.paper_bar_gap_fill_max_bars if max_fill_bars is None else max_fill_bars
        )
        if len(candles) < 2 or limit <= 0 or granularity <= 0:
            return list(candles), 0

        filled: list[Candle] = [candles[0]]
        inserted = 0
        for candle in candles[1:]:
            previous = filled[-1]
            delta = candle.ts - previous.ts
            if delta == granularity:
                filled.append(candle)
                continue
            if delta <= 0 or delta % granularity:
                # Duplicate, out-of-order, or unaligned — leave for validation.
                filled.append(candle)
                continue
            missing = delta // granularity - 1
            if missing > limit:
                filled.append(candle)
                continue
            price = previous.close
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

    def _usable_cached_candles(self, cached: _Cached | None, granularity: int) -> list[Candle]:
        if cached is None:
            return []
        valid, _ = self.validate_candles(cached.candles, granularity)
        return cached.candles if valid else []

    def candles(self, product_id: str, granularity: int | None = None) -> list[Candle]:
        """Return fresh, contiguous closed candles oldest-first, else an empty list."""
        gran = granularity or self.cfg.candle_granularity_seconds
        key = (product_id, gran)
        now = time.monotonic()
        ttl = self.cfg.cache_ttl_seconds
        if gran == self.cfg.paper_bar_granularity_seconds:
            ttl = min(ttl, self.cfg.paper_bar_cache_ttl_seconds)

        cached = self._candles.get(key)
        if cached is not None and now - cached.at < ttl:
            usable = self._usable_cached_candles(cached, gran)
            if usable:
                return usable
        if self._is_unavailable(product_id):
            return self._usable_cached_candles(cached, gran)

        # Coinbase Exchange does not offer 4h candles. Build UTC-aligned 4h
        # bars from its standard 1h feed instead of sending an invalid request.
        if gran == 14_400:
            hourly = self.candles(product_id, 3_600)
            parsed = [
                candle
                for candle in aggregate_candles(hourly, gran)
                if candle.ts + gran <= time.time()
            ]
            valid, detail = self.validate_candles(parsed, gran)
            if not valid:
                log.warning("market: rejected 4h candles for %s: %s", product_id, detail)
                return self._usable_cached_candles(cached, gran)
            self._candles[key] = _Cached(at=now, candles=parsed)
            return parsed
        if gran not in COINBASE_GRANULARITIES:
            log.warning("market: unsupported Coinbase candle granularity %s", gran)
            return []

        try:
            resp = self._client.get(
                f"{API_BASE}/products/{product_id}/candles",
                params={"granularity": gran},
            )
            if resp.status_code == 404:
                log.warning("market: product %s not listed on Coinbase", product_id)
                self._mark_unavailable(product_id)
                return []
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:  # noqa: BLE001 - never let market data crash the loop
            log.warning("market: candle fetch failed for %s: %s", product_id, exc)
            return self._usable_cached_candles(cached, gran)

        try:
            parsed = [
                Candle(
                    ts=int(r[0]),
                    low=float(r[1]),
                    high=float(r[2]),
                    open=float(r[3]),
                    close=float(r[4]),
                    volume=float(r[5]),
                )
                for r in rows
                if isinstance(r, list) and len(r) >= 6
            ]
        except (TypeError, ValueError) as exc:
            log.warning("market: malformed candle payload for %s: %s", product_id, exc)
            return self._usable_cached_candles(cached, gran)
        parsed.sort(key=lambda c: c.ts)
        # Strategies act on candle closes, never a still-forming Coinbase bar.
        parsed = [candle for candle in parsed if candle.ts + gran <= time.time()]
        if (
            gran == self.cfg.paper_bar_granularity_seconds
            and self.cfg.paper_bar_gap_fill_enabled
        ):
            parsed, inserted = self.fill_candle_gaps(parsed, gran)
            if inserted:
                log.info(
                    "market: filled %s missing %ss candle(s) for %s",
                    inserted,
                    gran,
                    product_id,
                )
        valid, detail = self.validate_candles(parsed, gran)
        if not valid:
            log.warning("market: rejected %s candles for %s: %s", gran, product_id, detail)
            return self._usable_cached_candles(cached, gran)
        self._candles[key] = _Cached(at=now, candles=parsed)
        return parsed

    def quote(self, product_id: str) -> TopOfBookQuote | None:
        """Fresh level-1 bid/ask and top-size, with no stale fallback."""
        now_mono = time.monotonic()
        hit = self._quotes.get(product_id)
        if (
            hit is not None
            and now_mono - hit[0] < self.cfg.price_cache_ttl_seconds
            and hit[1].age_seconds() <= self.cfg.paper_quote_max_age_seconds
        ):
            return hit[1]
        if self._is_unavailable(product_id):
            return None

        try:
            resp = self._client.get(
                f"{API_BASE}/products/{product_id}/book",
                params={"level": 1},
            )
            if resp.status_code == 404:
                self._mark_unavailable(product_id)
                return None
            resp.raise_for_status()
            payload = resp.json()
            bid_row = payload["bids"][0]
            ask_row = payload["asks"][0]
            quote = TopOfBookQuote(
                product_id=product_id,
                bid=float(bid_row[0]),
                ask=float(ask_row[0]),
                bid_size=float(bid_row[1]),
                ask_size=float(ask_row[1]),
                sequence=int(payload.get("sequence", 0)),
                observed_at=time.time(),
            )
            if (
                quote.bid <= 0
                or quote.ask <= 0
                or quote.bid_size <= 0
                or quote.ask_size <= 0
                or quote.ask < quote.bid
            ):
                raise ValueError("invalid top-of-book values")
        except Exception as exc:  # noqa: BLE001
            log.warning("market: quote fetch failed for %s: %s", product_id, exc)
            return None

        self._quotes[product_id] = (now_mono, quote)
        return quote

    def price(self, product_id: str) -> float | None:
        """Fresh top-of-book midpoint, or None; never a candle-close fallback."""
        quote = self.quote(product_id)
        return quote.midpoint if quote is not None else None

    def paper_bars(self, product_id: str, after_ts: int | None = None) -> list[Candle]:
        """Return a fresh contiguous one-minute PAPER bar walk after `after_ts`."""
        granularity = self.cfg.paper_bar_granularity_seconds
        candles = self.candles(product_id, granularity)
        valid, detail = self.validate_candles(candles, granularity)
        if not valid:
            raise MarketDataUnavailable(f"{product_id} paper bars unavailable: {detail}")
        if after_ts is None:
            return candles
        if after_ts > candles[-1].ts:
            raise MarketDataUnavailable(
                f"{product_id} paper bar cursor {after_ts} is ahead of latest {candles[-1].ts}"
            )
        newer = [candle for candle in candles if candle.ts > after_ts]
        if newer and newer[0].ts != after_ts + granularity:
            raise MarketDataUnavailable(
                f"{product_id} paper bars are not contiguous after cursor {after_ts}"
            )
        return newer

    # ---- Derived ------------------------------------------------------------

    def snapshot(
        self, product_id: str, sma_periods: int, lookback_periods: int
    ) -> TechnicalSnapshot:
        candles = self.candles(product_id)
        if len(candles) < 3:
            return TechnicalSnapshot(
                product_id=product_id, ok=False, detail="insufficient candle history"
            )

        price = self.price(product_id)
        if price is None or price <= 0:
            return TechnicalSnapshot(
                product_id=product_id, ok=False, detail="fresh top-of-book quote unavailable"
            )
        atr_abs = atr(candles, self.cfg.atr_periods)
        return TechnicalSnapshot(
            product_id=product_id,
            ok=True,
            price=price,
            atr=atr_abs,
            atr_pct=(atr_abs / price) if price > 0 else 0.0,
            trailing_return=trailing_return(candles, lookback_periods),
            sma=sma(candles, sma_periods),
            volume_z=volume_zscore(candles, self.cfg.confirmation.volume_periods),
            candles=len(candles),
            detail="ok",
        )

    def regime_ok(self) -> tuple[bool, str]:
        """True when the benchmark is in a RISK-ON state for new bull entries.

        Requires daily close above SMA(sma_periods). When configured, also
        rejects consecutive lower lows on the structure timeframe (default 4h).
        """
        cfg = self.cfg.regime
        if not cfg.enabled:
            return True, "regime filter disabled"

        candles = self.candles(cfg.benchmark_product_id, cfg.granularity_seconds)
        if len(candles) < cfg.sma_periods:
            detail = (
                f"{cfg.benchmark_product_id}: only {len(candles)} candles, need {cfg.sma_periods}"
            )
            return (not cfg.fail_closed), detail

        trend = sma(candles, cfg.sma_periods)
        last = candles[-1].close
        if trend <= 0:
            return (not cfg.fail_closed), "benchmark SMA unavailable"

        above = last > trend
        detail = (
            f"{cfg.benchmark_product_id} {last:.2f} "
            f"{'above' if above else 'below'} SMA{cfg.sma_periods} {trend:.2f}"
        )
        if not above:
            return False, detail

        if cfg.require_no_lower_lows:
            structure = self.candles(cfg.benchmark_product_id, cfg.structure_granularity_seconds)
            ok, structure_detail = _no_consecutive_lower_lows(
                structure, cfg.structure_lower_lows_bars
            )
            if not ok:
                if structure_detail.startswith("insufficient"):
                    return (not cfg.fail_closed), (
                        f"{detail}; 4h structure unavailable ({structure_detail})"
                    )
                return False, f"{detail}; {structure_detail}"
            detail = f"{detail}; {structure_detail}"
        return True, detail

    def close(self) -> None:
        self._client.close()


def _no_consecutive_lower_lows(candles: list[Candle], bars: int) -> tuple[bool, str]:
    """Return False when the last ``bars`` lows are strictly decreasing."""
    if bars < 2:
        return True, "lower-lows check disabled"
    if len(candles) < bars:
        return False, f"insufficient 4h history ({len(candles)}/{bars})"
    window = candles[-bars:]
    lows = [c.low for c in window]
    decreasing = all(later < earlier for earlier, later in zip(lows[:-1], lows[1:], strict=True))
    if decreasing:
        joined = " > ".join(f"{low:.2f}" for low in lows)
        return False, f"4h consecutive lower lows ({joined})"
    return True, f"4h lows not consecutively lower (last={lows[-1]:.2f})"
