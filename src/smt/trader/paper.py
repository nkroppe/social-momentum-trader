"""Paper broker: simulated prices and fills, no external calls.

Prices follow a per-product random walk seeded from a base price, so TP/SL/
time-stop logic exercises realistically during a soak.
"""

from __future__ import annotations

import random
import uuid

from ..config import get_risk
from ..logging_setup import get_logger
from .broker import Fill

log = get_logger("smt.broker.paper")

_BASE_PRICES = {
    "BTC-USD": 65000.0,
    "ETH-USD": 3200.0,
    "SOL-USD": 150.0,
    "DOGE-USD": 0.15,
    "XRP-USD": 0.55,
    "ADA-USD": 0.45,
    "AVAX-USD": 35.0,
    "LINK-USD": 15.0,
}


class PaperBroker:
    name = "paper"
    server_side_brackets = False

    def __init__(self, seed: int | None = None):
        self._rng = random.Random(seed)
        self._prices: dict[str, float] = dict(_BASE_PRICES)
        self._fee_pct = get_risk().assumed_fee_pct_per_side

    def current_price(self, product_id: str) -> float:
        price = self._prices.get(product_id)
        if price is None:
            price = 100.0
        # Random walk: +/- up to ~1.5% per tick.
        drift = self._rng.uniform(-0.015, 0.015)
        price = max(price * (1 + drift), 1e-6)
        self._prices[product_id] = price
        return price

    def open_long(
        self, product_id: str, notional_usd: float, tp_price: float, sl_price: float
    ) -> Fill:
        price = self.current_price(product_id)
        qty = notional_usd / price
        fee = notional_usd * self._fee_pct
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

    def close_long(self, product_id: str, qty: float) -> Fill:
        price = self.current_price(product_id)
        proceeds = qty * price
        fee = proceeds * self._fee_pct
        log.info("[paper] SELL %s qty=%.8f @ %.6f", product_id, qty, price)
        return Fill(order_id=f"paper-{uuid.uuid4().hex[:12]}", price=price, qty=qty, fee=fee)

    # Test/soak helper: force a price (used to trigger TP/SL deterministically).
    def set_price(self, product_id: str, price: float) -> None:
        self._prices[product_id] = price
