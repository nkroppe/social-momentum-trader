"""Live Coinbase Advanced Trade broker with fund-protection guardrails.

This is the ONLY supported live execution venue for social-momentum-trader.
Do not add alternate exchange adapters to this repo; future on-chain Solana
trading belongs in a separate project.

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
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from ..config import SecurityConfig, Settings
from ..logging_setup import get_logger
from .broker import Fill

log = get_logger("smt.broker.coinbase")


class TransferPermissionError(RuntimeError):
    """Raised when the API key can move funds (must be trade-only)."""


class ForbiddenApiPathError(RuntimeError):
    """Raised when a request path looks like a transfer/withdraw."""


def _field(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj and obj[name] not in (None, ""):
            return obj[name]
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value not in (None, ""):
                return value
    return default


def _as_mapping(obj: Any) -> Any:
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return obj
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        with contextlib.suppress(Exception):
            mapped = to_dict()
            if mapped is not None:
                return mapped
    return obj


def _unwrap_order(resp: Any) -> Any:
    data = _as_mapping(resp)
    if isinstance(data, dict):
        for key in ("order", "success_response"):
            nested = data.get(key)
            if nested:
                return nested
    order = getattr(resp, "order", None)
    if order is not None:
        return order
    success = getattr(resp, "success_response", None)
    if success is not None:
        return success
    return resp


def _order_id(resp: Any, fallback: str = "") -> str:
    order = _unwrap_order(resp)
    value = _field(order, "order_id", "orderId", default="")
    return str(value or fallback)


def _float_field(obj: Any, *names: str, default: float = 0.0) -> float:
    raw = _field(obj, *names, default=None)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


class CoinbaseBroker:
    name = "coinbase"
    server_side_brackets = True

    def __init__(
        self,
        settings: Settings,
        security: SecurityConfig,
        client: Any | None = None,
    ):
        self.settings = settings
        self.security = security
        if client is None:
            from coinbase.rest import RESTClient  # lazy import (live extra)

            self.client = RESTClient(
                api_key=settings.coinbase_api_key,
                api_secret=settings.coinbase_api_secret,
            )
        else:
            self.client = client
        self._protect_orders: dict[str, set[str]] = {}
        self._assert_trade_only()

    # ---- Guardrails --------------------------------------------------------

    def _assert_trade_only(self) -> None:
        """Layer 1: refuse to run unless the key is View+Trade with NO transfer."""
        perms = self.client.get_api_key_permissions()
        can_view = bool(_field(perms, "can_view", default=False))
        can_trade = bool(_field(perms, "can_trade", default=False))
        can_transfer = bool(_field(perms, "can_transfer", default=True))

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

    def _track(self, product_id: str, order_id: str) -> None:
        if order_id:
            self._protect_orders.setdefault(product_id, set()).add(order_id)

    def _untrack(self, product_id: str, order_ids: Iterable[str]) -> None:
        tracked = self._protect_orders.get(product_id)
        if not tracked:
            return
        tracked.difference_update(oid for oid in order_ids if oid)
        if not tracked:
            self._protect_orders.pop(product_id, None)

    # ---- Market data -------------------------------------------------------

    def current_price(self, product_id: str) -> float:
        self._guard_path(f"/products/{product_id}")
        product = self.client.get_product(product_id)
        return float(product["price"] if isinstance(product, dict) else product.price)

    # ---- Fill reconcile ----------------------------------------------------

    def reconcile_fill(
        self,
        order_id: str,
        *,
        fallback_price: float,
        fallback_qty: float,
    ) -> Fill:
        """Read average fill price/qty/fee from get_order; fall back if unfilled."""
        if not order_id:
            return Fill(order_id="", price=fallback_price, qty=fallback_qty, fee=0.0)
        self._guard_path(f"/orders/historical/{order_id}")
        resp = self.client.get_order(order_id)
        order = _unwrap_order(resp)
        price = _float_field(order, "average_filled_price", "averageFilledPrice")
        qty = _float_field(order, "filled_size", "filledSize")
        fee = _float_field(order, "total_fees", "totalFees")
        if price <= 0:
            price = fallback_price
        if qty <= 0:
            qty = fallback_qty
        return Fill(order_id=order_id, price=price, qty=qty, fee=max(fee, 0.0))

    def _list_open_order_ids(self, product_id: str) -> list[str]:
        self._guard_path("/orders/historical/batch")
        try:
            resp = self.client.list_orders(
                product_ids=[product_id],
                order_status=["OPEN", "PENDING"],
            )
        except Exception as exc:  # noqa: BLE001 - cancel still proceeds with known ids
            log.warning("list_orders failed for %s leftover brackets: %s", product_id, exc)
            return []
        data = _as_mapping(resp)
        if isinstance(data, dict):
            orders = data.get("orders", [])
        else:
            orders = getattr(resp, "orders", []) or []
        ids: list[str] = []
        for order in orders:
            oid = str(_field(order, "order_id", "orderId", default="") or "")
            if oid:
                ids.append(oid)
        return ids

    def cancel_leftover_brackets(
        self,
        product_id: str,
        order_ids: Sequence[str] | None = None,
    ) -> list[str]:
        """Cancel leftover TP/SL for this product (kill, time-stop, replace)."""
        wanted = {oid for oid in (order_ids or []) if oid}
        wanted.update(self._protect_orders.get(product_id, set()))
        wanted.update(self._list_open_order_ids(product_id))
        ids = [oid for oid in wanted if oid]
        if not ids:
            return []
        self._guard_path("/orders/batch_cancel")
        self.client.cancel_orders(order_ids=ids)
        self._untrack(product_id, ids)
        log.warning("[live] cancelled leftover brackets %s %s", product_id, ids)
        return ids

    def replace_remaining_bracket(
        self,
        product_id: str,
        qty: float,
        tp_price: float,
        sl_price: float,
    ) -> str:
        """Cancel leftover TP/SL and place a reduce-only bracket for remaining qty."""
        self.cancel_leftover_brackets(product_id)
        if qty <= 0:
            return ""
        self._guard_path("/orders")
        tp = tp_price
        if tp <= sl_price:
            tp = sl_price * 1.02 if sl_price > 0 else tp_price
        client_order_id = f"smt-{uuid.uuid4().hex}"
        resp = self.client.trigger_bracket_order_gtc_sell(
            client_order_id=client_order_id,
            product_id=product_id,
            base_size=str(qty),
            limit_price=str(tp),
            stop_trigger_price=str(sl_price),
        )
        order_id = _order_id(resp, fallback=client_order_id)
        self._track(product_id, order_id)
        log.warning(
            "[live] replace remaining bracket %s qty=%.8f tp=%.6f sl=%.6f oid=%s",
            product_id,
            qty,
            tp,
            sl_price,
            order_id,
        )
        return order_id

    # ---- Orders ------------------------------------------------------------

    def open_long(
        self, product_id: str, notional_usd: float, tp_price: float, sl_price: float
    ) -> Fill:
        """Market buy with an attached take-profit/stop-loss bracket."""
        self._guard_path("/orders")
        client_order_id = f"smt-{uuid.uuid4().hex}"
        # Attached bracket: server-side TP/SL that reduce/close the position.
        resp = self.client.trigger_bracket_order_gtc_buy(
            client_order_id=client_order_id,
            product_id=product_id,
            quote_size=str(round(notional_usd, 2)),
            limit_price=str(tp_price),
            stop_trigger_price=str(sl_price),
        )
        order_id = _order_id(resp, fallback=client_order_id)
        price = self.current_price(product_id)
        qty = notional_usd / price if price else 0.0
        fill = self.reconcile_fill(order_id, fallback_price=price, fallback_qty=qty)
        self._track(product_id, fill.order_id)
        log.warning(
            "[live] bracket BUY %s $%.2f oid=%s fill=%.6f qty=%.8f fee=%.4f",
            product_id,
            notional_usd,
            fill.order_id,
            fill.price,
            fill.qty,
            fill.fee,
        )
        return fill

    def close_long(
        self,
        product_id: str,
        qty: float,
        reference_price: float | None = None,
        *,
        emergency: bool = False,
    ) -> Fill:
        """Market sell after cancelling leftover TP/SL (kill / time-stop / partial)."""
        with contextlib.suppress(Exception):
            self.cancel_leftover_brackets(product_id)
        self._guard_path("/orders")
        client_order_id = f"smt-{uuid.uuid4().hex}"
        resp = self.client.market_order_sell(
            client_order_id=client_order_id,
            product_id=product_id,
            base_size=str(qty),
        )
        order_id = _order_id(resp, fallback=client_order_id)
        price = reference_price if reference_price and reference_price > 0 else self.current_price(
            product_id
        )
        fill = self.reconcile_fill(order_id, fallback_price=price, fallback_qty=qty)
        log.warning(
            "[live] SELL %s qty=%.8f oid=%s fill=%.6f fee=%.4f emergency=%s",
            product_id,
            fill.qty,
            fill.order_id,
            fill.price,
            fill.fee,
            emergency,
        )
        return fill

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
