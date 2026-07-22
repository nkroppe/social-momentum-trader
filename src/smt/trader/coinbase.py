"""Live Coinbase Advanced Trade broker with fund-protection guardrails.

Guardrails enforced here (Layers 1, 5 of the security plan):
  * On construction, assert the API key is TRADE-ONLY (can_transfer == False).
    If transfer is enabled, refuse to run.
  * Only order/market-data/account-read methods are ever called. No transfer/
    withdraw/convert/portfolio-move methods are imported or used.
  * A path guard rejects any product_id or request that resembles a transfer.

This path is gated behind LIVE=true AND a completed paper soak. It is not
exercised in paper mode.
"""

from __future__ import annotations

import contextlib

from ..config import SecurityConfig, Settings
from ..logging_setup import get_logger
from .broker import Fill

log = get_logger("smt.broker.coinbase")


class TransferPermissionError(RuntimeError):
    """Raised when the API key can move funds (must be trade-only)."""


class ForbiddenApiPathError(RuntimeError):
    """Raised when a request path looks like a transfer/withdraw."""


class CoinbaseBroker:
    name = "coinbase"
    server_side_brackets = True

    def __init__(self, settings: Settings, security: SecurityConfig):
        from coinbase.rest import RESTClient  # lazy import (live extra)

        self.settings = settings
        self.security = security
        self.client = RESTClient(
            api_key=settings.coinbase_api_key,
            api_secret=settings.coinbase_api_secret,
        )
        self._assert_trade_only()

    # ---- Guardrails --------------------------------------------------------

    def _assert_trade_only(self) -> None:
        """Layer 1: refuse to run unless the key is View+Trade with NO transfer."""
        perms = self.client.get_api_key_permissions()
        can_view = bool(getattr(perms, "can_view", False))
        can_trade = bool(getattr(perms, "can_trade", False))
        can_transfer = bool(getattr(perms, "can_transfer", True))

        detail = f"can_view={can_view} can_trade={can_trade} can_transfer={can_transfer}"
        if self.security.forbid_transfer_permission and can_transfer:
            raise TransferPermissionError(
                "Coinbase API key has TRANSFER permission enabled. "
                "Create a trade-only key (View+Trade) and disable Transfer. " + detail
            )
        if self.security.require_trade_only_key and not (can_view and can_trade):
            raise TransferPermissionError(
                "Coinbase API key must have View+Trade permissions. " + detail
            )
        log.warning("Coinbase key permission check passed: %s", detail)

    def _guard_path(self, path: str) -> None:
        """Layer 5: reject anything resembling a transfer/withdraw path."""
        lowered = path.lower()
        for bad in self.security.forbidden_api_path_substrings:
            if bad in lowered:
                raise ForbiddenApiPathError(
                    f"Blocked forbidden API path fragment '{bad}' in {path}"
                )

    # ---- Market data -------------------------------------------------------

    def current_price(self, product_id: str) -> float:
        self._guard_path(f"/products/{product_id}")
        product = self.client.get_product(product_id)
        return float(product["price"] if isinstance(product, dict) else product.price)

    # ---- Orders ------------------------------------------------------------

    def open_long(
        self, product_id: str, notional_usd: float, tp_price: float, sl_price: float
    ) -> Fill:
        """Market buy with an attached take-profit/stop-loss bracket."""
        self._guard_path("/orders")
        import uuid

        client_order_id = uuid.uuid4().hex
        # Attached bracket: server-side TP/SL that reduce/close the position.
        resp = self.client.trigger_bracket_order_gtc_buy(
            client_order_id=client_order_id,
            product_id=product_id,
            quote_size=str(round(notional_usd, 2)),
            limit_price=str(tp_price),
            stop_trigger_price=str(sl_price),
        )
        order = resp.get("success_response", resp) if isinstance(resp, dict) else resp
        order_id = str(
            getattr(order, "order_id", "")
            or (order.get("order_id") if isinstance(order, dict) else "")
        )
        price = self.current_price(product_id)
        qty = notional_usd / price
        log.warning("[live] bracket BUY %s $%.2f oid=%s", product_id, notional_usd, order_id)
        # NOTE (live TODO): reconcile actual fill price/qty/fee from get_order(order_id).
        return Fill(order_id=order_id or client_order_id, price=price, qty=qty, fee=0.0)

    def close_long(self, product_id: str, qty: float) -> Fill:
        """Market sell (used for time-stop / kill; TP/SL are server-side)."""
        self._guard_path("/orders")
        import uuid

        resp = self.client.market_order_sell(
            client_order_id=uuid.uuid4().hex,
            product_id=product_id,
            base_size=str(qty),
        )
        order = resp.get("success_response", resp) if isinstance(resp, dict) else resp
        order_id = str(
            getattr(order, "order_id", "")
            or (order.get("order_id") if isinstance(order, dict) else "")
        )
        price = self.current_price(product_id)
        log.warning("[live] SELL %s qty=%.8f oid=%s", product_id, qty, order_id)
        return Fill(order_id=order_id, price=price, qty=qty, fee=0.0)

    # ---- Monitoring hooks (used by ops.transfer monitor) -------------------

    def portfolio_equity_usd(self) -> float:
        self._guard_path("/accounts")
        total = 0.0
        accounts = self.client.get_accounts()
        items = accounts.get("accounts", []) if isinstance(accounts, dict) else accounts.accounts
        for acct in items:
            bal = acct["available_balance"] if isinstance(acct, dict) else acct.available_balance
            value = float(bal["value"] if isinstance(bal, dict) else bal.value)
            currency = bal["currency"] if isinstance(bal, dict) else bal.currency
            if currency in ("USD", "USDC"):
                total += value
            else:
                with contextlib.suppress(Exception):
                    total += value * self.current_price(f"{currency}-USD")
        return total
