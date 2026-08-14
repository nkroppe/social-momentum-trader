"""Shared PAPER execution-cost modeling for fills and pre-trade risk."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from ..config import MarketConfig, UniverseConfig
from ..market import TopOfBookQuote


class ExecutionCostError(ValueError):
    """The quoted trade cannot be modeled within configured constraints."""


@dataclass(frozen=True)
class ExecutionEstimate:
    price: float
    qty: float
    fee: float
    participation: float

    @property
    def gross_notional(self) -> float:
        return self.price * self.qty

    @property
    def net_proceeds(self) -> float:
        return self.gross_notional - self.fee


class ExecutionCostEstimator:
    """Model fee, spread, adverse slippage, and level-1 depth consistently."""

    def __init__(
        self,
        fee_pct_per_side: float,
        market_cfg: MarketConfig,
        universe: UniverseConfig | None = None,
    ):
        if not 0 <= fee_pct_per_side < 1:
            raise ValueError("fee_pct_per_side must be within 0..<1")
        self.fee_pct_per_side = fee_pct_per_side
        self.market_cfg = market_cfg
        self.universe = universe

    @property
    def adverse_slippage_fraction(self) -> float:
        return self.market_cfg.paper_adverse_slippage_bps / 10_000.0

    def validate_quote(self, quote: TopOfBookQuote, *, entry: bool = False) -> None:
        if (
            quote.bid <= 0
            or quote.ask <= 0
            or quote.ask < quote.bid
            or quote.bid_size <= 0
            or quote.ask_size <= 0
        ):
            raise ExecutionCostError(f"{quote.product_id}: invalid top-of-book quote")
        age = quote.age_seconds()
        if age > self.market_cfg.paper_quote_max_age_seconds:
            raise ExecutionCostError(
                f"{quote.product_id}: quote is {age:.1f}s old "
                f"(max {self.market_cfg.paper_quote_max_age_seconds:.1f}s)"
            )
        if entry and quote.spread_bps > self.market_cfg.paper_max_spread_bps + 1e-9:
            raise ExecutionCostError(
                f"{quote.product_id}: spread {quote.spread_bps:.1f}bps exceeds "
                f"{self.market_cfg.paper_max_spread_bps:.1f}bps"
            )

    def _check_depth(
        self,
        quote: TopOfBookQuote,
        *,
        side: str,
        qty: float,
        enforce: bool,
    ) -> float:
        size = quote.ask_size if side == "ask" else quote.bid_size
        level_notional = quote.ask_notional if side == "ask" else quote.bid_notional
        participation = qty / size
        if not enforce:
            return participation
        min_notional = self.market_cfg.min_top_level_notional_usd(
            self.universe.tier_of_product(quote.product_id) if self.universe is not None else None
        )
        if level_notional < min_notional:
            raise ExecutionCostError(
                f"{quote.product_id}: {side} depth ${level_notional:.2f} below "
                f"${min_notional:.2f}"
            )
        if participation > self.market_cfg.paper_max_top_level_participation:
            raise ExecutionCostError(
                f"{quote.product_id}: top-{side} participation {participation:.1%} exceeds "
                f"{self.market_cfg.paper_max_top_level_participation:.1%}"
            )
        return participation

    def estimate_buy(
        self,
        quote: TopOfBookQuote,
        notional_usd: float,
        *,
        enforce_depth: bool = True,
    ) -> ExecutionEstimate:
        self.validate_quote(quote, entry=True)
        if notional_usd <= 0:
            raise ExecutionCostError("buy notional must be positive")
        requested_qty = notional_usd / quote.ask
        participation = self._check_depth(
            quote,
            side="ask",
            qty=requested_qty,
            enforce=enforce_depth,
        )
        price = quote.ask * (1.0 + self.adverse_slippage_fraction)
        qty = notional_usd / price
        return ExecutionEstimate(
            price=price,
            qty=qty,
            fee=notional_usd * self.fee_pct_per_side,
            participation=participation,
        )

    def estimate_sell(
        self,
        quote: TopOfBookQuote,
        qty: float,
        *,
        reference_price: float | None = None,
        projected_reference: bool = False,
        enforce_depth: bool = False,
    ) -> ExecutionEstimate:
        self.validate_quote(quote)
        if qty <= 0:
            raise ExecutionCostError("sell quantity must be positive")
        participation = self._check_depth(
            quote,
            side="bid",
            qty=qty,
            enforce=enforce_depth,
        )
        if projected_reference:
            if reference_price is None or reference_price <= 0:
                raise ExecutionCostError("projected sell requires a positive reference price")
            executable_bid = reference_price * (quote.bid / quote.midpoint)
        else:
            executable_bid = quote.bid
            if reference_price is not None and reference_price > 0:
                executable_bid = min(executable_bid, reference_price)
        price = executable_bid * (1.0 - self.adverse_slippage_fraction)
        gross = price * qty
        return ExecutionEstimate(
            price=price,
            qty=qty,
            fee=gross * self.fee_pct_per_side,
            participation=participation,
        )

    def cost_adjusted_breakeven(
        self,
        entry_price: float,
        remaining_entry_fee_per_unit: float,
        quote: TopOfBookQuote,
    ) -> float:
        """Reference price whose modeled sale recovers entry and both-side costs."""
        self.validate_quote(quote)
        sell_discount = (
            (quote.bid / quote.midpoint)
            * (1.0 - self.adverse_slippage_fraction)
            * (1.0 - self.fee_pct_per_side)
        )
        if sell_discount <= 0:
            raise ExecutionCostError("modeled sell discount is not positive")
        return (entry_price + remaining_entry_fee_per_unit) / sell_discount


def conservative_quote(
    product_id: str,
    midpoint: float,
    spread_bps: float,
) -> TopOfBookQuote:
    """Synthetic max-spread quote for explicit offline/test fallback paths."""
    if midpoint <= 0:
        raise ExecutionCostError(f"{product_id}: invalid midpoint {midpoint}")
    half_spread = midpoint * spread_bps / 20_000.0
    return TopOfBookQuote(
        product_id=product_id,
        bid=midpoint - half_spread,
        ask=midpoint + half_spread,
        bid_size=math.inf,
        ask_size=math.inf,
        sequence=0,
        observed_at=time.time(),
    )
