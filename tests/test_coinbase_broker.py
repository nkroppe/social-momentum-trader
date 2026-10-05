"""Mocked Coinbase REST tests: fill reconcile, leftover cancel, cancel/replace."""

from __future__ import annotations

import logging
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from _helpers import make_store, make_strategy, make_universe

from smt.config import SecurityConfig, Settings
from smt.models import ExitReason, TradeStatus
from smt.trader.broker import Fill
from smt.trader.coinbase import (
    LIVE_STOP_LIMIT_BUFFER_PCT,
    CoinbaseBroker,
    ForbiddenApiPathError,
    PortfolioScopeError,
    TransferPermissionError,
    _round_down_to_increment,
)
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
        self.base_increment = "0.00000001"
        self.quote_increment = "0.01"

    def get_api_key_permissions(self):
        return self.perms

    def get_product(self, product_id, **_kwargs):
        self.calls.append(("get_product", product_id))
        return {
            "price": str(self.price),
            "base_increment": self.base_increment,
            "quote_increment": self.quote_increment,
        }

    def market_order_buy(self, **kwargs):
        self.calls.append(("market_buy", kwargs))
        oid = "entry-1"
        if oid not in self.open_ids:
            self.open_ids = [oid]
        return {"success_response": {"order_id": oid}}

    def trigger_bracket_order_gtc_buy(self, **kwargs):
        self.calls.append(("bracket_buy", kwargs))
        oid = "entry-1"
        self.open_ids = [oid]
        return {"success_response": {"order_id": oid}}

    def trigger_bracket_order_gtc_sell(self, **kwargs):
        self._seq += 1
        oid = f"protect-{self._seq}"
        self.calls.append(("bracket_sell", kwargs))
        if oid not in self.open_ids:
            self.open_ids.append(oid)
        return {"success_response": {"order_id": oid}}

    def stop_limit_order_gtc_sell(self, **kwargs):
        self._seq += 1
        oid = f"stop-{self._seq}"
        self.calls.append(("stop_limit_sell", kwargs))
        if oid not in self.open_ids:
            self.open_ids.append(oid)
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

    def get_accounts(self, **kwargs):
        self.calls.append(("get_accounts", kwargs))
        return {
            "accounts": [
                {"available_balance": {"value": "100.00", "currency": "USD"}},
            ]
        }


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
    buys = [call for call in client.calls if call[0] == "market_buy"]
    assert len(buys) == 1
    assert buys[0][1]["quote_size"] == "100.0"
    assert "base_size" not in buys[0][1]
    assert not any(call[0] == "bracket_buy" for call in client.calls)
    sells = [call for call in client.calls if call[0] == "bracket_sell"]
    assert len(sells) == 1
    assert Decimal(sells[0][1]["base_size"]) == Decimal("0.99502488")
    assert Decimal(sells[0][1]["limit_price"]) == Decimal("110")
    assert Decimal(sells[0][1]["stop_trigger_price"]) == Decimal("90")


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
    assert len(sell) >= 2  # entry protection + replacement
    replacement = sell[-1][1]
    assert Decimal(replacement["base_size"]) == Decimal("0.5")
    assert Decimal(replacement["limit_price"]) == Decimal("115")
    assert Decimal(replacement["stop_trigger_price"]) == Decimal("102")


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
    stop_calls = [call for call in rest.calls if call[0] == "stop_limit_sell"]
    assert stop_calls
    stop_kwargs = stop_calls[-1][1]
    assert "stop_trigger_price" not in stop_kwargs
    assert Decimal(stop_kwargs["limit_price"]) < Decimal(stop_kwargs["stop_price"])
    assert stop_kwargs["stop_direction"] == "STOP_DIRECTION_STOP_DOWN"
    assert open_trade.broker_entry_order_id.startswith("stop-")
    # Replacement must not re-arm the already-hit take-profit as a limit.
    assert Decimal(stop_kwargs["limit_price"]) != Decimal(str(open_trade.take_profit))


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

        def replace_remaining_stop(self, product_id, qty, stop_price):
            self.replaces.append((product_id, qty, None, stop_price))
            return f"stop-{len(self.replaces)}"

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


def test_missing_portfolio_id_raises_before_any_client_call():
    rest = FakeREST()
    settings = Settings(
        coinbase_api_key="test-key",
        coinbase_api_secret="test-secret",
        coinbase_portfolio_id="",
        paper_start_equity=5_000,
    )
    with pytest.raises(PortfolioScopeError, match="COINBASE_PORTFOLIO_ID"):
        CoinbaseBroker(settings, _security(), client=rest)
    assert rest.calls == []
    assert rest.perms.can_view is True

    blank = Settings(
        coinbase_api_key="test-key",
        coinbase_api_secret="test-secret",
        coinbase_portfolio_id="   ",
        paper_start_equity=5_000,
    )
    with pytest.raises(PortfolioScopeError, match="COINBASE_PORTFOLIO_ID"):
        CoinbaseBroker(blank, _security(), client=FakeREST())


def test_live_preflight_empty_portfolio_id_hardens_doctor_detail(monkeypatch, tmp_path):
    from smt.ops.preflight import run_preflight

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("smt.ops.preflight._market_data_checks", lambda: [])
    settings = Settings(
        coinbase_api_key="test-key",
        coinbase_api_secret="test-secret",
        coinbase_portfolio_id="",
        paper_start_equity=5_000,
    )
    monkeypatch.setattr("smt.ops.preflight.get_settings", lambda: settings)
    results = {row.name: row for row in run_preflight("live")}
    creds = results["coinbase_credentials"]
    assert creds.passed is False
    assert "COINBASE_PORTFOLIO_ID is empty" in creds.detail
    assert "refuses unscoped" in creds.detail

    blank = settings.model_copy(update={"coinbase_portfolio_id": "   "})
    monkeypatch.setattr("smt.ops.preflight.get_settings", lambda: blank)
    blank_creds = {row.name: row for row in run_preflight("live")}["coinbase_credentials"]
    assert blank_creds.passed is False
    assert "COINBASE_PORTFOLIO_ID is empty" in blank_creds.detail


def test_scoped_kwargs_inject_retail_portfolio_id_on_required_methods():
    broker, client = _broker()
    scoped = broker._scoped(product_ids=["BTC-USD"])
    assert scoped == {
        "product_ids": ["BTC-USD"],
        "retail_portfolio_id": "portfolio-1",
    }

    broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    broker.close_long("BTC-USD", 0.50, reference_price=99.0)
    broker.replace_remaining_bracket("BTC-USD", 0.50, 115.0, 102.0)
    assert broker.portfolio_equity_usd() == pytest.approx(100.0)

    required = {
        "market_buy",
        "bracket_sell",
        "market_sell",
        "list_orders",
        "get_accounts",
    }
    unscoped = {"get_product", "get_order", "cancel_orders"}
    seen: set[str] = set()
    for kind, payload in client.calls:
        if kind in required:
            seen.add(kind)
            assert payload["retail_portfolio_id"] == "portfolio-1"
        if kind in unscoped and isinstance(payload, dict):
            assert "retail_portfolio_id" not in payload
    assert seen == required


def test_call_scoped_typeerror_propagates_unchanged():
    class RejectKwarg(FakeREST):
        def market_order_buy(self, **kwargs):
            self.calls.append(("market_buy", kwargs))
            raise TypeError("got an unexpected keyword argument 'quote_size'")

        def get_accounts(self, **kwargs):
            self.calls.append(("get_accounts", kwargs))
            raise TypeError("got an unexpected keyword argument 'bogus'")

        def list_orders(self, **kwargs):
            self.calls.append(("list_orders", kwargs))
            raise TypeError("got an unexpected keyword argument 'bogus'")

    rest = RejectKwarg()
    broker, _ = _broker(rest)
    with pytest.raises(TypeError, match="quote_size"):
        broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    buys = [call for call in rest.calls if call[0] == "market_buy"]
    assert len(buys) == 1
    assert buys[0][1]["retail_portfolio_id"] == "portfolio-1"

    with pytest.raises(TypeError, match="bogus"):
        broker.portfolio_equity_usd()
    accounts = [call for call in rest.calls if call[0] == "get_accounts"]
    assert len(accounts) == 1
    assert accounts[0][1]["retail_portfolio_id"] == "portfolio-1"

    with pytest.raises(TypeError, match="bogus"):
        broker._list_open_order_ids("BTC-USD")
    listed = [call for call in rest.calls if call[0] == "list_orders"]
    assert len(listed) == 1
    assert listed[0][1]["retail_portfolio_id"] == "portfolio-1"


def test_call_scoped_method_without_portfolio_param_raises_before_call():
    called: list[str] = []

    def no_scope(product_ids, order_status):
        called.append("list")
        return {"orders": []}

    broker, _client = _broker()
    with pytest.raises(PortfolioScopeError, match="refusing unscoped"):
        broker._call_scoped(no_scope, product_ids=["BTC-USD"], order_status=["OPEN"])
    assert called == []


def test_call_scoped_magicmock_is_treated_as_accepting():
    method = MagicMock(return_value={"ok": True})
    broker, _client = _broker()
    assert broker._call_scoped(method, product_id="BTC-USD") == {"ok": True}
    assert method.call_args.kwargs["retail_portfolio_id"] == "portfolio-1"
    assert method.call_args.kwargs["product_id"] == "BTC-USD"


def test_round_down_to_increment_plain_decimal_strings():
    assert _round_down_to_increment("0.123456789", "0.00000001") == "0.12345678"
    assert _round_down_to_increment("0.123456789", "0.001") == "0.123"
    assert _round_down_to_increment("110.019", "0.01") == "110.01"
    assert _round_down_to_increment("1e-7", "1e-8") == "0.0000001"
    assert "e" not in _round_down_to_increment("1e-7", "1e-8").lower()
    assert Decimal(_round_down_to_increment(0.4, "1")) == Decimal("0")


def test_open_long_rounds_base_and_prices_to_increments():
    rest = FakeREST()
    rest.base_increment = "0.00000001"
    rest.quote_increment = "0.01"
    rest.buy_fill = {
        "order_id": "entry-1",
        "average_filled_price": "100.50",
        "filled_size": "0.123456789",
        "total_fees": "0.60",
    }
    broker, client = _broker(rest)
    broker.open_long("BTC-USD", 100.0, 110.019, 90.019)
    sell = [call for call in client.calls if call[0] == "bracket_sell"][0][1]
    assert sell["base_size"] == "0.12345678"
    assert sell["limit_price"] == "110.01"
    assert sell["stop_trigger_price"] == "90.01"


def test_open_long_rounds_base_size_to_coarse_increment():
    rest = FakeREST()
    rest.base_increment = "0.001"
    rest.buy_fill = {
        "order_id": "entry-1",
        "average_filled_price": "100.50",
        "filled_size": "0.123456789",
        "total_fees": "0.60",
    }
    broker, client = _broker(rest)
    broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    sell = [call for call in client.calls if call[0] == "bracket_sell"][0][1]
    assert sell["base_size"] == "0.123"


def test_open_long_skips_bracket_when_rounded_size_is_zero(caplog):
    rest = FakeREST()
    rest.base_increment = "1"
    rest.buy_fill = {
        "order_id": "entry-1",
        "average_filled_price": "100.50",
        "filled_size": "0.4",
        "total_fees": "0.60",
    }
    broker, client = _broker(rest)
    with caplog.at_level(logging.ERROR):
        fill = broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    assert fill.order_id == "entry-1"
    assert fill.qty == pytest.approx(0.4)
    assert not any(call[0] == "bracket_sell" for call in client.calls)
    assert "rounded base size is 0" in caplog.text


def test_open_long_retries_unfilled_get_order(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(
        "smt.trader.coinbase._fill_retry_sleep",
        lambda seconds: sleeps.append(seconds),
    )

    class SlowFill(FakeREST):
        def __init__(self):
            super().__init__()
            self.gets = 0

        def get_order(self, order_id):
            self.gets += 1
            self.calls.append(("get_order", order_id))
            if self.gets < 3:
                return {
                    "order": {
                        "order_id": order_id,
                        "average_filled_price": "0",
                        "filled_size": "0",
                        "total_fees": "0",
                    }
                }
            return {"order": dict(self.buy_fill, order_id=order_id)}

    rest = SlowFill()
    broker, _ = _broker(rest)
    fill = broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    assert rest.gets == 3
    assert len(sleeps) == 2
    assert fill.qty == pytest.approx(0.99502488)
    sell = [call for call in rest.calls if call[0] == "bracket_sell"]
    assert Decimal(sell[0][1]["base_size"]) == Decimal("0.99502488")


def test_open_long_fill_fallback_logs_warning(monkeypatch, caplog):
    monkeypatch.setattr("smt.trader.coinbase._fill_retry_sleep", lambda _seconds: None)

    class NeverFill(FakeREST):
        def get_order(self, order_id):
            self.calls.append(("get_order", order_id))
            return {
                "order": {
                    "order_id": order_id,
                    "average_filled_price": "0",
                    "filled_size": "0",
                    "total_fees": "0",
                }
            }

    rest = NeverFill()
    rest.price = 100.0
    broker, _ = _broker(rest)
    with caplog.at_level(logging.WARNING):
        fill = broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    assert fill.qty == pytest.approx(1.0)
    assert fill.price == pytest.approx(100.0)
    assert "falling back" in caplog.text


def test_open_long_bracket_failure_returns_entry_fill(caplog):
    class BoomBracket(FakeREST):
        def trigger_bracket_order_gtc_sell(self, **kwargs):
            self.calls.append(("bracket_sell", kwargs))
            raise RuntimeError("exchange down")

    rest = BoomBracket()
    broker, _ = _broker(rest)
    with caplog.at_level(logging.CRITICAL):
        fill = broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    assert fill.order_id == "entry-1"
    assert fill.qty == pytest.approx(0.99502488)
    assert "protection bracket failed" in caplog.text
    assert "entry-1" in broker._protect_orders.get("BTC-USD", set())


def test_replace_remaining_stop_places_stop_limit_below_stop():
    broker, client = _broker()
    broker.open_long("BTC-USD", 100.0, 110.0, 90.0)
    new_id = broker.replace_remaining_stop("BTC-USD", 0.50, 102.0)
    assert new_id.startswith("stop-")
    stops = [call for call in client.calls if call[0] == "stop_limit_sell"]
    assert stops
    kwargs = stops[-1][1]
    assert Decimal(kwargs["base_size"]) == Decimal("0.5")
    assert Decimal(kwargs["stop_price"]) == Decimal("102")
    expected_limit = Decimal("102") * (Decimal("1") - Decimal(str(LIVE_STOP_LIMIT_BUFFER_PCT)))
    assert Decimal(kwargs["limit_price"]) == Decimal(
        _round_down_to_increment(expected_limit, "0.01")
    )
    assert Decimal(kwargs["limit_price"]) < Decimal(kwargs["stop_price"])
    assert kwargs["stop_direction"] == "STOP_DIRECTION_STOP_DOWN"
    assert "take_profit" not in kwargs
    assert "stop_trigger_price" not in kwargs


def test_manager_partial_taken_syncs_stop_only_not_take_profit(tmp_path):
    class LiveBroker:
        name = "coinbase"
        server_side_brackets = True

        def __init__(self):
            self.price = 100.0
            self.stops: list[tuple] = []
            self.brackets: list[tuple] = []

        def current_price(self, _product_id):
            return self.price

        def open_long(self, _product, notional, _tp, _sl):
            return Fill("entry-1", 100.0, notional / 100.0, 0.60)

        def close_long(self, _product, qty, reference_price=None, *, emergency=False):
            return Fill("sell-1", reference_price or self.price, qty, 0.30)

        def cancel_leftover_brackets(self, product_id, order_ids=None):
            return list(order_ids or [])

        def replace_remaining_bracket(self, product_id, qty, tp_price, sl_price):
            self.brackets.append((product_id, qty, tp_price, sl_price))
            return "protect-1"

        def replace_remaining_stop(self, product_id, qty, stop_price):
            self.stops.append((product_id, qty, stop_price))
            return "stop-1"

    store = make_store(tmp_path)
    broker = LiveBroker()
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
    trade = store.open_trade_for("BTC", strategy.name)
    assert trade is not None
    trade.partial_taken = True
    trade.qty = 5.0
    trade.take_profit = 115.0
    trade.trailing_stop = 101.5
    manager._sync_protecting_bracket(trade)
    assert broker.brackets == []
    assert broker.stops == [("BTC-USD", 5.0, 101.5)]
    assert trade.broker_entry_order_id == "stop-1"


def test_paper_broker_unaffected_by_live_replace_methods(tmp_path):
    from smt.trader.paper import PaperBroker

    store = make_store(tmp_path)
    broker = PaperBroker(seed=1, offline_simulation=True)
    strategy = make_strategy(advanced_exit_enabled=True)
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        strategies=[strategy],
    )
    trade = manager.open_position(_candidate(), 100.0, strategy)
    assert trade.status == TradeStatus.OPEN
    assert not callable(getattr(broker, "replace_remaining_bracket", None))
    assert not callable(getattr(broker, "replace_remaining_stop", None))
    manager._sync_protecting_bracket(trade)
    still = store.open_trade_for("BTC", strategy.name)
    assert still is not None
    assert still.broker_entry_order_id == trade.broker_entry_order_id
