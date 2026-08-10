"""Paper broker with executable level-1 fills and an explicit offline simulator."""

from __future__ import annotations

import random
import uuid

from ..config import MarketConfig, get_market, get_risk
from ..logging_setup import get_logger
from ..market import Candle, MarketData, MarketDataUnavailable, TopOfBookQuote
from .broker import Fill
from .execution import (
    ExecutionCostError,
    ExecutionCostEstimator,
    ExecutionEstimate,
    conservative_quote,
)

log = get_logger("smt.broker.paper")

_BASE_PRICES = {
    "BTC-USD": 65000.0,
    "ETH-USD": 3200.0,
    "SOL-USD": 150.0,
    "DOGE-USD": 0.15,
    "XRP-USD": 0.55,
    "ADA-USD": 0.45,
    "AVAX-USD": 35.0,
}


class PaperOrderRejected(RuntimeError):
    """A PAPER entry cannot be executed within configured market constraints."""


class PaperMarketUnavailable(RuntimeError):
    """Fresh market data required by deployed PAPER is unavailable."""


class PaperBroker:
    name = "paper"
    server_side_brackets = False

    def __init__(
        self,
        seed: int | None = None,
        market: MarketData | None = None,
        *,
        offline_simulation: bool = False,
        market_cfg: MarketConfig | None = None,
    ):
        self._rng = random.Random(seed)
        self._prices: dict[str, float] = dict(_BASE_PRICES)
        self._pinned: dict[str, float] = {}
        self._market = market
        # A seed is an explicit test/simulation input retained for compatibility.
        self.offline_simulation = offline_simulation or seed is not None
        if market is None and not self.offline_simulation:
            raise ValueError("deployed PAPER requires MarketData; use offline_simulation=True")
        self._cfg = market_cfg or (market.cfg if market is not None else get_market())
        self._fee_pct = get_risk().assumed_fee_pct_per_side
        self._costs = ExecutionCostEstimator(self._fee_pct, self._cfg)
        self._last_closed_bar: dict[str, int] = {}

    def _fresh_quote(self, product_id: str) -> TopOfBookQuote:
        if self._market is None:
            raise PaperMarketUnavailable(f"{product_id}: market provider unavailable")
        quote = self._market.quote(product_id)
        if quote is None:
            raise PaperMarketUnavailable(f"{product_id}: fresh top-of-book quote unavailable")
        age = quote.age_seconds()
        if age > self._cfg.paper_quote_max_age_seconds:
            raise PaperMarketUnavailable(
                f"{product_id}: quote is {age:.1f}s old "
                f"(max {self._cfg.paper_quote_max_age_seconds:.1f}s)"
            )
        return quote

    def _offline_price(self, product_id: str) -> float:
        pinned = self._pinned.get(product_id)
        if pinned is not None:
            return pinned
        price = self._prices.get(product_id, 100.0)
        # Seeded random walk is intentionally confined to simulate/tests.
        drift = self._rng.uniform(-0.015, 0.015)
        price = max(price * (1 + drift), 1e-6)
        self._prices[product_id] = price
        return price

    def current_price(self, product_id: str) -> float:
        if self.offline_simulation:
            return self._offline_price(product_id)
        return self._fresh_quote(product_id).midpoint

    def _validate_entry_market(self, product_id: str, notional_usd: float) -> ExecutionEstimate:
        if self._market is None:
            raise PaperMarketUnavailable(f"{product_id}: market provider unavailable")
        try:
            bars = self._market.paper_bars(product_id)
        except MarketDataUnavailable as exc:
            raise PaperMarketUnavailable(str(exc)) from exc
        self._last_closed_bar[product_id] = bars[-1].ts

        quote = self._fresh_quote(product_id)
        try:
            return self._costs.estimate_buy(quote, notional_usd)
        except ExecutionCostError as exc:
            raise PaperOrderRejected(str(exc)) from exc

    def open_long(
        self, product_id: str, notional_usd: float, tp_price: float, sl_price: float
    ) -> Fill:
        if self.offline_simulation:
            price = self.current_price(product_id)
            qty = notional_usd / price
            fee = notional_usd * self._fee_pct
        else:
            estimate = self._validate_entry_market(product_id, notional_usd)
            price = estimate.price
            qty = estimate.qty
            fee = estimate.fee
        log.info(
            "[paper] BUY %s notional=$%.2f @ %.6f qty=%.8f (tp=%.6f sl=%.6f)",
            product_id,
            notional_usd,
            price,
            qty,
            tp_price,
            sl_price,
        )
        return Fill(order_id=f"paper-{uuid.uuid4().hex[:12]}", price=price, qty=qty, fee=fee)

    def close_long(
        self,
        product_id: str,
        qty: float,
        reference_price: float | None = None,
    ) -> Fill:
        if self.offline_simulation:
            executable_price = self.current_price(product_id)
            price = (
                min(executable_price, reference_price)
                if reference_price is not None and reference_price > 0
                else executable_price
            )
        else:
            quote = self._fresh_quote(product_id)
            estimate = self._costs.estimate_sell(
                quote,
                qty,
                reference_price=reference_price,
            )
            price = estimate.price
            fee = estimate.fee
        if self.offline_simulation:
            proceeds = qty * price
            fee = proceeds * self._fee_pct
        log.info("[paper] SELL %s qty=%.8f @ %.6f", product_id, qty, price)
        return Fill(order_id=f"paper-{uuid.uuid4().hex[:12]}", price=price, qty=qty, fee=fee)

    def execution_quote(
        self,
        product_id: str,
        fallback_midpoint: float | None = None,
    ) -> TopOfBookQuote:
        """Fresh live quote, or a conservative explicit-simulation substitute."""
        if not self.offline_simulation:
            return self._fresh_quote(product_id)
        midpoint = fallback_midpoint or self.current_price(product_id)
        return conservative_quote(
            product_id,
            midpoint,
            self._cfg.paper_max_spread_bps,
        )

    def closed_bars_since(self, product_id: str, after_ts: int) -> list[Candle]:
        if self.offline_simulation or self._market is None:
            return []
        try:
            return self._market.paper_bars(product_id, after_ts)
        except MarketDataUnavailable as exc:
            raise PaperMarketUnavailable(str(exc)) from exc

    def last_closed_bar_ts(self, product_id: str) -> int:
        cached = self._last_closed_bar.get(product_id, 0)
        if cached > 0 or self.offline_simulation or self._market is None:
            return cached
        try:
            bars = self._market.paper_bars(product_id)
        except MarketDataUnavailable as exc:
            raise PaperMarketUnavailable(str(exc)) from exc
        latest = bars[-1].ts
        self._last_closed_bar[product_id] = latest
        return latest

    # Test/simulate helper: pin a price so TP/SL trigger deterministically.
    def set_price(self, product_id: str, price: float) -> None:
        if not self.offline_simulation:
            raise RuntimeError("set_price is only available in offline simulation")
        self._pinned[product_id] = price
        self._prices[product_id] = price
