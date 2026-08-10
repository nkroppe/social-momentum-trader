"""Broker abstraction shared by paper and live executors."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..config import Settings, get_security
from ..logging_setup import get_logger
from ..market import MarketData

log = get_logger("smt.broker")


@dataclass
class Fill:
    order_id: str
    price: float
    qty: float
    fee: float


@runtime_checkable
class Broker(Protocol):
    name: str
    # True if TP/SL are enforced server-side (live); False if the loop must
    # simulate them by polling price (paper).
    server_side_brackets: bool

    def current_price(self, product_id: str) -> float: ...

    def open_long(
        self, product_id: str, notional_usd: float, tp_price: float, sl_price: float
    ) -> Fill: ...

    def close_long(
        self,
        product_id: str,
        qty: float,
        reference_price: float | None = None,
    ) -> Fill: ...


def build_broker(
    settings: Settings,
    market: MarketData | None = None,
    *,
    offline_simulation: bool = False,
) -> Broker:
    """Return a live Coinbase broker if LIVE and fully configured, else paper.

    The live path enforces the trade-only-key guardrails on construction and
    will raise if the API key can transfer funds. `market` lets the paper broker
    quote real prices so soak results reflect the live market.
    """
    if settings.live and settings.coinbase_configured:
        from .coinbase import CoinbaseBroker

        log.warning("LIVE mode: constructing Coinbase broker with guardrails")
        return CoinbaseBroker(settings, get_security())

    if settings.live and not settings.coinbase_configured:
        raise RuntimeError("LIVE=true but Coinbase API not configured. Refusing to start.")

    from .paper import PaperBroker

    if market is None and not offline_simulation:
        raise RuntimeError("PAPER startup blocked: fresh MarketData is required")

    source = "real Coinbase level-1 book" if market is not None else "explicit offline simulation"
    log.info("PAPER mode: constructing simulated broker (%s)", source)
    return PaperBroker(market=market, offline_simulation=offline_simulation)
