"""Bind live broker kwargs against coinbase-advanced-py 1.8.4 RESTClient signatures."""

from __future__ import annotations

import importlib.metadata
import inspect
from types import SimpleNamespace

import pytest

pytest.importorskip("coinbase.rest")

from coinbase.rest import RESTClient  # noqa: E402

from smt.config import SecurityConfig, Settings
from smt.trader.coinbase import CoinbaseBroker

_REQUIRED_SDK_VERSION = "1.8.4"


def _require_sdk_version() -> None:
    try:
        version = importlib.metadata.version("coinbase-advanced-py")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("coinbase-advanced-py is not installed")
    if version != _REQUIRED_SDK_VERSION:
        pytest.skip(f"coinbase-advanced-py {version} != {_REQUIRED_SDK_VERSION}")


def _security() -> SecurityConfig:
    return SecurityConfig(
        require_trade_only_key=True,
        forbid_transfer_permission=True,
        forbidden_api_path_substrings=[
            "withdraw",
            "transfer",
            "payment",
            "sweep",
            "convert",
            "address",
        ],
    )


def _settings() -> Settings:
    return Settings(
        coinbase_api_key="test-key",
        coinbase_api_secret="test-secret",
        coinbase_portfolio_id="test-portfolio",
        paper_start_equity=5_000,
    )


def _named_parameters(signature: inspect.Signature) -> set[str]:
    return {
        name
        for name, param in signature.parameters.items()
        if name != "self"
        and param.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }


class RecordingREST:
    """Fake client that records (method_name, kwargs) for every SDK call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self._seq = 0
        self.perms = SimpleNamespace(can_view=True, can_trade=True, can_transfer=False)

    def get_api_key_permissions(self, **kwargs):
        self.calls.append(("get_api_key_permissions", dict(kwargs)))
        return self.perms

    def __getattr__(self, name: str):
        def method(*args, **kwargs):
            recorded = dict(kwargs)
            signature = inspect.signature(getattr(RESTClient, name))
            positional = [
                param
                for param in signature.parameters.values()
                if param.name != "self" and param.kind != inspect.Parameter.VAR_KEYWORD
            ]
            for arg, param in zip(args, positional, strict=False):
                recorded.setdefault(param.name, arg)
            self.calls.append((name, recorded))
            return self._response(name, recorded)

        method.__name__ = name
        return method

    def _response(self, name: str, kwargs: dict):
        if name == "get_product":
            return {
                "price": "100.50",
                "base_increment": "0.00000001",
                "quote_increment": "0.01",
            }
        if name == "get_order":
            return {
                "order": {
                    "order_id": kwargs.get("order_id", "oid"),
                    "average_filled_price": "100.50",
                    "filled_size": "0.99502488",
                    "total_fees": "0.60",
                }
            }
        if name == "list_orders":
            return {"orders": [{"order_id": "entry-1"}]}
        if name == "get_accounts":
            return {
                "accounts": [
                    {"available_balance": {"value": "100.00", "currency": "USD"}},
                ]
            }
        if name == "cancel_orders":
            ids = kwargs.get("order_ids") or []
            return {"results": [{"success": True, "order_id": oid} for oid in ids]}
        self._seq += 1
        return {"success_response": {"order_id": f"oid-{self._seq}"}}


def test_recorded_live_calls_bind_to_real_sdk_named_parameters():
    _require_sdk_version()
    client = RecordingREST()
    broker = CoinbaseBroker(_settings(), _security(), client=client)

    broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    broker.replace_remaining_bracket("BTC-USD", 0.50, 115.0, 102.0)
    broker.replace_remaining_stop("BTC-USD", 0.50, 102.0)
    broker.close_long("BTC-USD", 0.50, reference_price=99.0)
    broker.cancel_leftover_brackets("BTC-USD")
    assert broker.portfolio_equity_usd() == pytest.approx(100.0)
    broker.reconcile_fill("entry-1", fallback_price=100.0, fallback_qty=1.0)

    assert client.calls
    exercised = {name for name, _kwargs in client.calls}
    for required in (
        "market_order_buy",
        "trigger_bracket_order_gtc_sell",
        "stop_limit_order_gtc_sell",
        "market_order_sell",
        "get_product",
        "get_order",
        "list_orders",
        "get_accounts",
        "cancel_orders",
    ):
        assert required in exercised, f"missing recorded call {required}"

    for name, kwargs in client.calls:
        real = getattr(RESTClient, name)
        signature = inspect.signature(real)
        signature.bind(None, **kwargs)
        named = _named_parameters(signature)
        absorbed = sorted(key for key in kwargs if key not in named)
        assert absorbed == [], f"{name} kwargs absorbed by **kwargs: {absorbed} from {kwargs}"
