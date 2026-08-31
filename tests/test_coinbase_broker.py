"""Mocked Coinbase REST tests: fill reconcile, leftover cancel, cancel/replace."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from _helpers import make_store, make_strategy, make_universe

from smt.config import SecurityConfig, Settings
from smt.models import ExitReason, TradeStatus
from smt.trader.broker import Fill
from smt.trader.coinbase import CoinbaseBroker, ForbiddenApiPathError, TransferPermissionError
from smt.trader.manager import TradeManager
from smt.trader.signals import TradeCandidate


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
        coinbase_portfolio_id="portfolio-1",
        paper_start_equity=5_000,
    )


def _candidate(**overrides) -> TradeCandidate:
    values = dict(
        ticker="BTC",
        product_id="BTC-USD",
        zscore=3.0,
        mentions=10,
        sources=1,
        reason="test",
        strategy="intraday",
        setup="breakout_close",
        entry_price=100.0,
        structure_stop=90.0,
        stop_pct=0.10,
    )
    values.update(overrides)
    return TradeCandidate(**values)


class FakeREST:
    def __init__(self, *, can_transfer: bool = False, price: float = 100.0):
        self.price = price
        self.calls: list[tuple] = []
        self.buy_fill = {
            "order_id": "entry-1",
            "average_filled_price": "100.50",
            "filled_size": "0.99502488",
            "total_fees": "0.60",
        }
        self.sell_fill = {
            "order_id": "sell-1",
            "average_filled_price": "99.00",
            "filled_size": "0.50",
            "total_fees": "0.30",
        }
        self.open_ids = ["entry-1"]
        self._seq = 1
        self.perms = SimpleNamespace(can_view=True, can_trade=True, can_transfer=can_transfer)

    def get_api_key_permissions(self):
        return self.perms

    def get_product(self, product_id):
        self.calls.append(("get_product", product_id))
        return {"price": str(self.price)}

    def trigger_bracket_order_gtc_buy(self, **kwargs):
        self.calls.append(("bracket_buy", kwargs))
        oid = "entry-1"
        self.open_ids = [oid]
        return {"success_response": {"order_id": oid}}

    def trigger_bracket_order_gtc_sell(self, **kwargs):
        self._seq += 1
        oid = f"protect-{self._seq}"
        self.calls.append(("bracket_sell", kwargs))
        self.open_ids = [oid]
        return {"success_response": {"order_id": oid}}

    def get_order(self, order_id):
        self.calls.append(("get_order", order_id))
        if str(order_id).startswith("sell") or str(order_id).startswith("protect"):
            fill = dict(self.sell_fill, order_id=order_id)
        else:
            fill = dict(self.buy_fill, order_id=order_id)
        return {"order": fill}

    def market_order_sell(self, **kwargs):
        self._seq += 1
        oid = f"sell-{self._seq}"
        self.calls.append(("market_sell", kwargs))
        self.sell_fill = dict(self.sell_fill, order_id=oid)
        return {"success_response": {"order_id": oid}}

    def cancel_orders(self, order_ids):
        ids = list(order_ids)
        self.calls.append(("cancel_orders", ids))
        self.open_ids = [oid for oid in self.open_ids if oid not in ids]
        return {"results": [{"success": True, "order_id": oid} for oid in ids]}

    def list_orders(self, **kwargs):
        self.calls.append(("list_orders", kwargs))
        return {"orders": [{"order_id": oid} for oid in self.open_ids]}


def _broker(rest: FakeREST | None = None) -> tuple[CoinbaseBroker, FakeREST]:
    client = rest or FakeREST()
    return CoinbaseBroker(_settings(), _security(), client=client), client


def test_trade_only_key_rejects_transfer_permission():
    rest = FakeREST(can_transfer=True)
    with pytest.raises(TransferPermissionError):
        CoinbaseBroker(_settings(), _security(), client=rest)


def test_open_long_reconciles_fill_from_get_order():
    broker, client = _broker()
    fill = broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    assert fill.order_id == "entry-1"
    assert fill.price == pytest.approx(100.50)
    assert fill.qty == pytest.approx(0.99502488)
    assert fill.fee == pytest.approx(0.60)
    assert any(call[0] == "get_order" and call[1] == "entry-1" for call in client.calls)
    assert any(call[0] == "bracket_buy" for call in client.calls)


def test_close_long_cancels_leftover_brackets_then_reconciles_sell():
    broker, client = _broker()
    broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    fill = broker.close_long("BTC-USD", 0.50, reference_price=99.0)
    cancel_calls = [call for call in client.calls if call[0] == "cancel_orders"]
    assert cancel_calls
    assert "entry-1" in cancel_calls[0][1]
    assert fill.price == pytest.approx(99.0)
    assert fill.fee == pytest.approx(0.30)
    assert any(call[0] == "market_sell" for call in client.calls)


def test_replace_remaining_bracket_cancels_then_places_sell_bracket():
    broker, client = _broker()
    broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    new_id = broker.replace_remaining_bracket("BTC-USD", 0.50, 115.0, 102.0)
    assert new_id.startswith("protect-")
    kinds = [call[0] for call in client.calls]
    assert kinds.count("cancel_orders") >= 1
    sell = [call for call in client.calls if call[0] == "bracket_sell"]
    assert sell
    assert sell[0][1]["base_size"] == "0.5"
    assert sell[0][1]["limit_price"] == "115.0"
    assert sell[0][1]["stop_trigger_price"] == "102.0"


def test_path_guard_blocks_transfer_fragment():
    broker, _client = _broker()
    with pytest.raises(ForbiddenApiPathError):
        broker._guard_path("/accounts/transfer")


def test_manager_kill_and_time_stop_cancel_leftover_brackets(tmp_path):
    rest = FakeREST()
    broker, _ = _broker(rest)
    store = make_store(tmp_path)
    strategy = make_strategy(advanced_exit_enabled=True)
    manager = TradeManager(
        _settings(),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
    )
    trade = manager.open_position(_candidate(), 100.0, strategy)
    assert trade.status == TradeStatus.OPEN
    assert trade.entry_price == pytest.approx(100.50)

    manager.manage_open_trades(force_flatten=True)
    closed = store.closed_trades_for("BTC", strategy.name)[-1]
    assert closed.exit_reason == ExitReason.KILL_SWITCH
    assert any(call[0] == "cancel_orders" for call in rest.calls)
    assert any(call[0] == "market_sell" for call in rest.calls)


def test_manager_partial_and_chandelier_replace_remaining_bracket(tmp_path):
    rest = FakeREST()
    broker, _ = _broker(rest)
    store = make_store(tmp_path)
    strategy = make_strategy(
        advanced_exit_enabled=True,
        partial_take_profit_fraction=0.5,
        partial_take_profit_r=1.5,
        chandelier_atr_mult=3.0,
    )
    manager = TradeManager(
        _settings(),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
    )
    trade = manager.open_position(_candidate(), 100.0, strategy)
    rest.price = 120.0
    rest.sell_fill = {
        "order_id": "sell-partial",
        "average_filled_price": "120.00",
        "filled_size": str(trade.qty * 0.5),
        "total_fees": "0.30",
    }
    manager.manage_open_trades()
    open_trade = store.open_trade_for("BTC", strategy.name)
    assert open_trade is not None
    assert open_trade.partial_taken
    assert any(call[0] == "bracket_sell" for call in rest.calls)
    assert open_trade.broker_entry_order_id.startswith("protect-")


def test_manager_chandelier_ratchet_replaces_stop(tmp_path):
    class LiveishBroker:
        name = "coinbase"
        server_side_brackets = True

        def __init__(self):
            self.price = 100.0
            self.replaces: list[tuple] = []
            self.cancels: list[str] = []

        def current_price(self, _product_id):
            return self.price

        def open_long(self, _product, notional, _tp, _sl):
            return Fill("entry-1", 100.0, notional / 100.0, 0.60)

        def close_long(self, _product, qty, reference_price=None, *, emergency=False):
            return Fill("sell-1", reference_price or self.price, qty, 0.30)

        def cancel_leftover_brackets(self, product_id, order_ids=None):
            self.cancels.append(product_id)
            return list(order_ids or [])

        def replace_remaining_bracket(self, product_id, qty, tp_price, sl_price):
            self.replaces.append((product_id, qty, tp_price, sl_price))
            return f"protect-{len(self.replaces)}"

    store = make_store(tmp_path)
    broker = LiveishBroker()
    strategy = make_strategy(
        advanced_exit_enabled=True,
        partial_take_profit_fraction=0.5,
        partial_take_profit_r=1.5,
        chandelier_atr_mult=3.0,
    )
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
    )
    manager.open_position(_candidate(), 1_000.0, strategy)
    broker.price = 115.0
    manager.manage_open_trades()
    open_trade = store.open_trade_for("BTC", strategy.name)
    assert open_trade is not None
    assert open_trade.partial_taken
    assert broker.replaces
    first = open_trade.trailing_stop
    broker.price = 130.0
    manager.manage_open_trades()
    open_trade = store.open_trade_for("BTC", strategy.name)
    assert open_trade is not None
    if open_trade.trailing_stop > first:
        assert len(broker.replaces) >= 2
