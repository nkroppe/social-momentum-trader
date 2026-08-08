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
from .indicators import Candle, atr, sma, trailing_return, volume_zscore

log = get_logger("smt.market")

API_BASE = "https://api.exchange.coinbase.com"


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
        self._prices: dict[str, tuple[float, float]] = {}
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

    def candles(self, product_id: str, granularity: int | None = None) -> list[Candle]:
        """Return cached hourly (or `granularity`) candles, oldest-first."""
        gran = granularity or self.cfg.candle_granularity_seconds
        key = (product_id, gran)
        now = time.monotonic()

        cached = self._candles.get(key)
        if cached is not None and now - cached.at < self.cfg.cache_ttl_seconds:
            return cached.candles
        if self._is_unavailable(product_id):
            return cached.candles if cached else []

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
            return cached.candles if cached else []

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
        parsed.sort(key=lambda c: c.ts)
        self._candles[key] = _Cached(at=now, candles=parsed)
        return parsed

    def price(self, product_id: str) -> float | None:
        """Latest trade price, or None when unavailable."""
        now = time.monotonic()
        hit = self._prices.get(product_id)
        if hit is not None and now - hit[0] < self.cfg.price_cache_ttl_seconds:
            return hit[1]
        if self._is_unavailable(product_id):
            return None

        try:
            resp = self._client.get(f"{API_BASE}/products/{product_id}/ticker")
            if resp.status_code == 404:
                self._mark_unavailable(product_id)
                return None
            resp.raise_for_status()
            price = float(resp.json()["price"])
        except Exception as exc:  # noqa: BLE001
            log.warning("market: price fetch failed for %s: %s", product_id, exc)
            # Fall back to the most recent candle close before giving up.
            candles = self._candles.get((product_id, self.cfg.candle_granularity_seconds))
            if candles and candles.candles:
                return candles.candles[-1].close
            return None

        self._prices[product_id] = (now, price)
        return price

    # ---- Derived ------------------------------------------------------------

    def snapshot(self, product_id: str, sma_periods: int, lookback_periods: int) -> (
        TechnicalSnapshot
    ):
        candles = self.candles(product_id)
        if len(candles) < 3:
            return TechnicalSnapshot(
                product_id=product_id, ok=False, detail="insufficient candle history"
            )

        price = self.price(product_id) or candles[-1].close
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
        """True when the benchmark (BTC) is above its trend moving average."""
        cfg = self.cfg.regime
        if not cfg.enabled:
            return True, "regime filter disabled"

        candles = self.candles(cfg.benchmark_product_id, cfg.granularity_seconds)
        if len(candles) < cfg.sma_periods:
            detail = (
                f"{cfg.benchmark_product_id}: only {len(candles)} candles, "
                f"need {cfg.sma_periods}"
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
        return above, detail

    def close(self) -> None:
        self._client.close()
