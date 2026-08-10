"""Network-free tests for fail-closed PAPER market data and execution."""

from __future__ import annotations

import time

import httpx
import pytest
from _helpers import make_store, make_strategy, make_universe
from sqlalchemy import inspect

from smt.config import MarketConfig, Settings, get_risk
from smt.market import Candle, MarketData, MarketDataUnavailable, TopOfBookQuote
from smt.models import ExitReason
from smt.trader.manager import TradeManager
from smt.trader.paper import PaperBroker, PaperOrderRejected
from smt.trader.signals import TradeCandidate


class StubCoinbaseClient:
    def __init__(
        self,
        book: dict,
        candle_rows: list[list[float]],
        *,
        fail_book: bool = False,
    ):
        self.book = book
        self.candle_rows = candle_rows
        self.fail_book = fail_book

    def get(self, url: str, params: dict | None = None) -> httpx.Response:
        payload = self.book if url.endswith("/book") else self.candle_rows
        request = httpx.Request("GET", url, params=params)
        if url.endswith("/book") and self.fail_book:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=payload, request=request)

    def close(self) -> None:
        pass


def _book(*, bid: float = 100.0, ask: float = 100.1, size: float = 20.0) -> dict:
    return {
        "sequence": 123,
        "bids": [[str(bid), str(size), 1]],
        "asks": [[str(ask), str(size), 1]],
    }


def _bar_row(
    ts: int,
    *,
    low: float = 99.0,
    high: float = 101.0,
    open_price: float = 100.0,
    close_price: float = 100.0,
) -> list[float]:
    return [ts, low, high, open_price, close_price, 10.0]


def _recent_rows() -> list[list[float]]:
    minute = int(time.time()) // 60 * 60
    return [_bar_row(ts) for ts in range(minute - 300, minute - 60, 60)]


def _market(
    *,
    book: dict | None = None,
    rows: list[list[float]] | None = None,
    **overrides,
) -> tuple[MarketData, StubCoinbaseClient]:
    values = {
        "paper_bar_max_age_seconds": 125.0,
        "paper_bar_cache_ttl_seconds": 1e-6,
        **overrides,
    }
    cfg = MarketConfig(**values)
    client = StubCoinbaseClient(book or _book(), rows or _recent_rows())
    return MarketData(cfg, client=client), client


def test_top_of_book_quote_is_typed_with_spread_and_depth():
    market, _ = _market(book=_book(bid=100.0, ask=100.2, size=3.0))

    quote = market.quote("BTC-USD")

    assert isinstance(quote, TopOfBookQuote)
    assert quote.midpoint == pytest.approx(100.1)
    assert quote.spread_bps == pytest.approx(19.98001998)
    assert quote.bid_notional == pytest.approx(300.0)
    assert quote.ask_notional == pytest.approx(300.6)


def test_stale_quote_is_not_returned_when_refresh_fails():
    market, client = _market()
    market._quotes["BTC-USD"] = (  # noqa: SLF001 - assert cache safety directly
        time.monotonic(),
        TopOfBookQuote("BTC-USD", 100.0, 100.1, 10.0, 10.0, 1, time.time() - 30),
    )
    client.fail_book = True

    assert market.quote("BTC-USD") is None
    assert market.price("BTC-USD") is None


def test_candle_validation_rejects_stale_and_gapped_windows():
    market, _ = _market()
    now = int(time.time())
    stale_ts = now // 60 * 60 - 600
    stale = [Candle(stale_ts, 99, 101, 100, 100, 1)]
    gapped = [
        Candle(now // 60 * 60 - 180, 99, 101, 100, 100, 1),
        Candle(now // 60 * 60 - 60, 99, 101, 100, 100, 1),
    ]

    assert market.validate_candles(stale, 60, now=now)[0] is False
    assert market.validate_candles(gapped, 60, now=now)[0] is False
    stale_market, _ = _market(
        rows=[_bar_row(stale_ts)],
        paper_bar_max_age_seconds=90.0,
    )
    with pytest.raises(MarketDataUnavailable):
        stale_market.paper_bars("BTC-USD")


@pytest.mark.parametrize(
    ("book", "notional", "reason"),
    [
        (_book(bid=99.0, ask=100.0, size=20.0), 100.0, "spread"),
        (_book(bid=99.99, ask=100.0, size=0.5), 20.0, "ask depth"),
        (_book(bid=99.99, ask=100.0, size=5.0), 300.0, "participation"),
    ],
)
def test_paper_entry_rejects_unexecutable_top_of_book(book, notional, reason):
    market, _ = _market(book=book)
    broker = PaperBroker(market=market)

    with pytest.raises(PaperOrderRejected, match=reason):
        broker.open_long("BTC-USD", notional, 110.0, 90.0)


def test_paper_fills_buy_ask_and_sell_bid_with_adverse_slippage():
    market, _ = _market(book=_book(bid=100.0, ask=100.1, size=20.0))
    broker = PaperBroker(market=market)

    buy = broker.open_long("BTC-USD", 500.0, 110.0, 90.0)
    sell = broker.close_long("BTC-USD", buy.qty)

    assert buy.price == pytest.approx(100.1 * 1.0005)
    assert sell.price == pytest.approx(100.0 * 0.9995)
    assert buy.qty == pytest.approx(500.0 / buy.price)


def test_recovered_bid_cannot_improve_paper_fill_above_reference():
    market, _ = _market(book=_book(bid=110.0, ask=110.1, size=20.0))
    broker = PaperBroker(market=market)

    sell = broker.close_long("BTC-USD", 1.0, reference_price=100.0)

    assert sell.price == pytest.approx(100.0 * 0.9995)


def test_deployed_paper_requires_market_but_seeded_simulation_remains_explicit():
    with pytest.raises(ValueError, match="requires MarketData"):
        PaperBroker()

    simulated = PaperBroker(seed=7)
    simulated.set_price("BTC-USD", 123.0)
    assert simulated.current_price("BTC-USD") == 123.0


def test_paper_exit_walk_is_stop_first_and_persists_bar_cursor(tmp_path):
    market, client = _market()
    broker = PaperBroker(market=market)
    store = make_store(tmp_path)
    strategy = make_strategy(advanced_exit_enabled=False, exit_style="fixed")
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        market,
        market.cfg,
        strategies=[strategy],
    )
    candidate = TradeCandidate("BTC", "BTC-USD", 5.0, 20, 3, "x", strategy.name)
    trade = manager.open_position(candidate, 500.0, strategy)
    original_cursor = trade.last_processed_paper_bar_ts

    next_ts = original_cursor + 60
    client.candle_rows.append(
        _bar_row(
            next_ts,
            low=trade.stop_loss - 1.0,
            high=trade.take_profit + 1.0,
        )
    )
    manager.manage_open_trades()

    closed = store.closed_trades_for("BTC", strategy.name)[-1]
    assert closed.exit_reason == ExitReason.STOP_LOSS
    assert closed.last_processed_paper_bar_ts == next_ts
    assert closed.exit_price == pytest.approx(trade.stop_loss * 0.9995)
    columns = {column["name"] for column in inspect(store.engine).get_columns("trades")}
    assert "last_processed_paper_bar_ts" in columns


def test_legacy_paper_trade_initializes_cursor_without_replaying_old_bar(tmp_path):
    market, client = _market()
    broker = PaperBroker(market=market)
    store = make_store(tmp_path)
    strategy = make_strategy(advanced_exit_enabled=False, exit_style="fixed")
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        market,
        market.cfg,
        strategies=[strategy],
    )
    candidate = TradeCandidate("BTC", "BTC-USD", 5.0, 20, 3, "x", strategy.name)
    trade = manager.open_position(candidate, 500.0, strategy)
    latest_ts = trade.last_processed_paper_bar_ts
    trade.last_processed_paper_bar_ts = 0
    store.update_trade(trade)
    client.candle_rows[-1] = _bar_row(
        latest_ts,
        low=trade.stop_loss - 1.0,
        high=trade.take_profit + 1.0,
    )
    market._candles.clear()  # noqa: SLF001 - force the changed test bars

    manager.manage_open_trades()

    still_open = store.open_trade_for("BTC", strategy.name)
    assert still_open is not None
    assert still_open.last_processed_paper_bar_ts == latest_ts

    next_ts = latest_ts + 60
    client.candle_rows.append(_bar_row(next_ts, low=trade.stop_loss - 1.0))
    market._candles.clear()  # noqa: SLF001 - force the changed test bars
    manager.manage_open_trades()
    assert store.closed_trades_for("BTC", strategy.name)[-1].exit_reason == ExitReason.STOP_LOSS


def test_paper_exit_uses_fresh_quote_when_bar_walk_is_unavailable(tmp_path):
    market, client = _market()
    broker = PaperBroker(market=market)
    store = make_store(tmp_path)
    strategy = make_strategy(advanced_exit_enabled=False, exit_style="fixed")
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        market,
        market.cfg,
        strategies=[strategy],
    )
    candidate = TradeCandidate("BTC", "BTC-USD", 5.0, 20, 3, "x", strategy.name)
    trade = manager.open_position(candidate, 500.0, strategy)
    client.candle_rows = [
        client.candle_rows[0],
        client.candle_rows[-1],
    ]
    stopped_bid = trade.stop_loss - 1.0
    client.book = _book(bid=stopped_bid, ask=stopped_bid + 0.1, size=20.0)
    market._candles.clear()  # noqa: SLF001 - force the gapped test bars
    market._quotes.clear()  # noqa: SLF001 - force the changed test quote

    manager.manage_open_trades()

    closed = store.closed_trades_for("BTC", strategy.name)[-1]
    assert closed.exit_reason == ExitReason.STOP_LOSS
    assert closed.exit_price == pytest.approx(stopped_bid * 0.9995)


def test_recovered_quote_cannot_improve_paper_target_exit(tmp_path):
    market, client = _market()
    broker = PaperBroker(market=market)
    store = make_store(tmp_path)
    strategy = make_strategy(advanced_exit_enabled=False, exit_style="fixed")
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        market,
        market.cfg,
        strategies=[strategy],
    )
    candidate = TradeCandidate("BTC", "BTC-USD", 5.0, 20, 3, "x", strategy.name)
    trade = manager.open_position(candidate, 500.0, strategy)
    next_ts = trade.last_processed_paper_bar_ts + 60
    client.candle_rows.append(
        _bar_row(
            next_ts,
            low=trade.stop_loss + 1.0,
            high=trade.take_profit + 1.0,
        )
    )
    recovered_bid = trade.take_profit * 1.20
    client.book = _book(bid=recovered_bid, ask=recovered_bid + 0.1, size=20.0)
    market._quotes.clear()  # noqa: SLF001 - force the test's changed top of book

    manager.manage_open_trades()

    closed = store.closed_trades_for("BTC", strategy.name)[-1]
    assert closed.exit_reason == ExitReason.TAKE_PROFIT
    assert closed.exit_price == pytest.approx(trade.take_profit * 0.9995)


def test_paper_partial_fill_is_capped_at_partial_target(tmp_path):
    market, client = _market()
    broker = PaperBroker(market=market)
    store = make_store(tmp_path)
    strategy = make_strategy(
        advanced_exit_enabled=True,
        exit_style="fixed",
        partial_take_profit_fraction=0.5,
    )
    manager = TradeManager(
        Settings(paper_start_equity=5_000),
        make_universe(),
        store,
        broker,
        market,
        market.cfg,
        strategies=[strategy],
    )
    candidate = TradeCandidate(
        "BTC",
        "BTC-USD",
        5.0,
        20,
        3,
        "x",
        strategy.name,
        setup="breakout_close",
        entry_price=100.0,
        structure_stop=90.0,
        stop_pct=0.10,
    )
    trade = manager.open_position(candidate, 500.0, strategy)
    next_ts = trade.last_processed_paper_bar_ts + 60
    client.candle_rows.append(
        _bar_row(
            next_ts,
            low=trade.entry_price + 2.0,
            high=trade.take_profit + 1.0,
            open_price=trade.entry_price + 5.0,
            close_price=trade.entry_price + 5.0,
        )
    )
    recovered_bid = trade.take_profit * 1.20
    client.book = _book(bid=recovered_bid, ask=recovered_bid + 0.1, size=20.0)
    market._quotes.clear()  # noqa: SLF001 - force the test's changed top of book

    manager.manage_open_trades()

    partial = store.open_trade_for("BTC", strategy.name)
    assert partial is not None and partial.partial_taken
    partial_qty = trade.original_qty * strategy.partial_take_profit_fraction
    expected_fill = trade.take_profit * 0.9995
    expected_sell_fee = partial_qty * expected_fill * get_risk().assumed_fee_pct_per_side
    expected_entry_fee = trade.entry_fee_paid * strategy.partial_take_profit_fraction
    expected_pnl = (
        (expected_fill - trade.entry_price) * partial_qty - expected_sell_fee - expected_entry_fee
    )
    assert partial.partial_realized_pnl == pytest.approx(expected_pnl)
    assert partial.last_processed_paper_bar_ts == next_ts
