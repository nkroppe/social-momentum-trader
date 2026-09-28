"""Network-free tests for public Coinbase candle fetch + replay CSVs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from _helpers import make_store

from smt.backtest import load_candle_csv, parse_utc
from smt.cli import build_parser
from smt.config import get_market
from smt.market.data import API_BASE, COINBASE_GRANULARITIES
from smt.market.indicators import Candle
from smt.models import ExitReason, Trade, TradeStatus
from smt.ops.candle_fetch import (
    FetchResult,
    candle_csv_path,
    fetch_candles,
    fetch_trade_candles,
    fill_gaps,
    format_fetch_line,
    source_granularity,
    trade_candle_window,
    write_candle_csv,
)
from smt.ops.reports import GEN8_CONFIG_FINGERPRINT_PREFIX
from smt.trader.exit_policy import legacy_profile, resolve_profile

GEN8_FP = GEN8_CONFIG_FINGERPRINT_PREFIX + "a" * (64 - len(GEN8_CONFIG_FINGERPRINT_PREFIX))
HOUR = 3600
FOUR_H = 14_400
FAR_FUTURE = datetime(2030, 1, 1, tzinfo=UTC)


def _noop_sleep(_: float) -> None:
    return None


def _row(
    ts: int,
    *,
    open_: float | None = None,
    high: float | None = None,
    low: float | None = None,
    close: float | None = None,
    volume: float = 1.0,
) -> list[float]:
    price = 100.0 + ts / HOUR
    open_px = open_ if open_ is not None else price
    close_px = close if close is not None else open_px + 0.5
    return [
        ts,
        low if low is not None else open_px - 0.5,
        high if high is not None else close_px + 0.5,
        open_px,
        close_px,
        volume,
    ]


class CandleTransport(httpx.MockTransport):
    """Public-candle mock: records requests and serves newest-first rows."""

    def __init__(
        self,
        *,
        skip: set[int] | None = None,
        fail_products: set[str] | None = None,
        status_by_product: dict[str, int] | None = None,
    ):
        self.requests: list[httpx.Request] = []
        self.skip = skip or set()
        self.fail_products = fail_products or set()
        self.status_by_product = status_by_product or {}

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            product = request.url.path.split("/")[2]
            if product in self.fail_products:
                code = self.status_by_product.get(product, 500)
                return httpx.Response(code, json={"message": "unavailable"})
            params = dict(request.url.params)
            gran = int(params["granularity"])
            start = parse_utc(params["start"])
            end = parse_utc(params["end"])
            rows = [
                _row(ts)
                for ts in range(start, end + 1, gran)
                if ts not in self.skip
            ]
            rows.reverse()
            return httpx.Response(200, json=rows)

        super().__init__(handler)


def _client(transport: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=transport, headers={"User-Agent": "test-agent"})


def _closed_trade(
    store,
    *,
    ticker: str = "SOL",
    product_id: str = "SOL-USD",
    strategy: str = "intraday",
    opened_at: datetime,
    closed_at: datetime,
    fingerprint: str = GEN8_FP,
    exit_snapshot: dict | None = None,
) -> Trade:
    return store.add_trade(
        Trade(
            ticker=ticker,
            strategy=strategy,
            product_id=product_id,
            is_live=False,
            status=TradeStatus.CLOSED,
            qty=1.0,
            original_qty=1.0,
            entry_price=100.0,
            entry_notional=100.0,
            take_profit=110.0,
            stop_loss=90.0,
            time_stop_at=closed_at,
            exit_price=105.0,
            exit_reason=ExitReason.TAKE_PROFIT,
            realized_pnl=5.0,
            config_fingerprint=fingerprint,
            exit_snapshot=exit_snapshot,
            opened_at=opened_at,
            closed_at=closed_at,
        )
    )


def _request_window(request: httpx.Request) -> tuple[int, int, int, str]:
    params = dict(request.url.params)
    return (
        parse_utc(params["start"]),
        parse_utc(params["end"]),
        int(params["granularity"]),
        request.url.path.split("/")[2],
    )


def test_trade_candle_window_warmup_timestop_and_now_cap():
    opened = datetime(2026, 1, 1, 12, 7, tzinfo=UTC)
    closed = datetime(2026, 1, 1, 13, 0, tzinfo=UTC)
    trade = Trade(
        ticker="SOL",
        strategy="intraday",
        product_id="SOL-USD",
        qty=1.0,
        entry_price=100.0,
        entry_notional=100.0,
        take_profit=110.0,
        stop_loss=90.0,
        time_stop_at=closed,
        opened_at=opened,
        closed_at=closed,
    )
    profile = legacy_profile("intraday")
    atr_periods = 14
    g = profile.trail_granularity_seconds
    assert g == 900

    start, end, gran = trade_candle_window(trade, profile, atr_periods, now=FAR_FUTURE)
    assert gran == g
    opened_ts = int(opened.timestamp())
    closed_ts = int(closed.timestamp())
    floor_open = opened_ts // g * g
    assert start == floor_open - (atr_periods + 5) * g
    hard_stop = opened_ts + profile.time_stop_hours * 3600
    assert hard_stop > closed_ts
    ceil_end = hard_stop if hard_stop % g == 0 else (hard_stop // g + 1) * g
    assert end == ceil_end + 2 * g

    now = datetime(2026, 1, 1, 16, 7, tzinfo=UTC)
    _, capped, _ = trade_candle_window(trade, profile, atr_periods, now=now)
    last_closed_end = int(now.timestamp()) // g * g
    assert capped == last_closed_end
    assert capped < end


def test_fetch_candles_pages_within_300_and_covers_window():
    start = 1_700_006_400
    end = start + 400 * HOUR
    transport = CandleTransport()
    with _client(transport) as client:
        candles = fetch_candles(
            client,
            "BTC-USD",
            HOUR,
            start,
            end,
            sleep=_noop_sleep,
            pause_s=0.0,
        )

    assert len(transport.requests) >= 2
    windows = [_request_window(req) for req in transport.requests]
    max_bars = 300
    for req_start, req_end, gran, product in windows:
        assert product == "BTC-USD"
        assert gran == HOUR
        assert req_end > req_start
        # Inclusive-end: a span of max_bars * g would return max_bars + 1 bars.
        assert (req_end - req_start) < max_bars * gran
        last = (req_end // gran) * gran
        n_inclusive = (last - req_start) // gran + 1
        assert 1 <= n_inclusive <= max_bars

    covered: set[int] = set()
    for req_start, req_end, gran, _ in windows:
        last = (req_end // gran) * gran
        covered.update(range(req_start, last + 1, gran))
    needed = set(range(start, end, HOUR))
    assert needed <= covered
    assert min(req[0] for req in windows) <= start
    assert max(req[1] for req in windows) >= end - 1
    assert [c.ts for c in candles] == list(range(start, end, HOUR))
    assert len(candles) == 400


def test_fetch_candles_inclusive_end_span_cannot_return_more_than_max_bars():
    """Coinbase `end` is inclusive; a 300-hour span would return 301 hourly bars."""
    start = 1_700_006_400
    max_bars = 300
    end = start + max_bars * HOUR
    transport = CandleTransport()
    with _client(transport) as client:
        candles = fetch_candles(
            client,
            "BTC-USD",
            HOUR,
            start,
            end,
            max_bars_per_request=max_bars,
            sleep=_noop_sleep,
            pause_s=0.0,
        )

    assert transport.requests
    source_g = source_granularity(HOUR)
    for req_start, req_end, gran, _ in [_request_window(req) for req in transport.requests]:
        assert gran == source_g
        assert (req_end - req_start) < max_bars * source_g
        last = (req_end // gran) * gran
        n_inclusive = (last - req_start) // gran + 1
        assert n_inclusive <= max_bars
    assert [c.ts for c in candles] == list(range(start, end, HOUR))
    assert len(candles) == max_bars


def test_fetch_candles_aggregates_4h_from_1h():
    start = 1_700_006_400
    assert start % FOUR_H == 0
    end = start + 2 * FOUR_H
    transport = CandleTransport()
    with _client(transport) as client:
        candles = fetch_candles(
            client,
            "ETH-USD",
            FOUR_H,
            start,
            end,
            sleep=_noop_sleep,
            pause_s=0.0,
        )

    assert source_granularity(FOUR_H) == HOUR
    assert FOUR_H not in COINBASE_GRANULARITIES
    assert transport.requests
    for _, _, gran, product in [_request_window(req) for req in transport.requests]:
        assert product == "ETH-USD"
        assert gran == HOUR

    assert [c.ts for c in candles] == [start, start + FOUR_H]
    first = candles[0]
    hours = [start + i * HOUR for i in range(4)]
    expected_open = 100.0 + hours[0] / HOUR
    expected_close = 100.0 + hours[3] / HOUR + 0.5
    assert first.open == pytest.approx(expected_open)
    assert first.close == pytest.approx(expected_close)
    assert first.high == pytest.approx(expected_close + 0.5)
    assert first.low == pytest.approx(expected_open - 0.5)
    assert first.volume == pytest.approx(4.0)


def test_fill_gaps_inserts_flat_zero_volume_bars():
    candles = [
        Candle(ts=0, low=99.0, high=101.0, open=100.0, close=100.5, volume=3.0),
        Candle(ts=3 * HOUR, low=102.0, high=104.0, open=103.0, close=103.5, volume=2.0),
    ]
    filled, count = fill_gaps(candles, HOUR)
    assert count == 2
    assert [c.ts for c in filled] == [0, HOUR, 2 * HOUR, 3 * HOUR]
    for gap in filled[1:3]:
        assert gap.open == gap.high == gap.low == gap.close == 100.5
        assert gap.volume == 0.0
    assert filled[-1].close == 103.5


def test_write_candle_csv_round_trips_load_candle_csv(tmp_path: Path):
    candles = [
        Candle(
            ts=1_700_006_400 + i * HOUR,
            low=99.0 + i,
            high=101.5 + i,
            open=100.0 + i,
            close=100.5 + i,
            volume=1.5,
        )
        for i in range(5)
    ]
    path = tmp_path / "BTC-USD.csv"
    write_candle_csv(path, candles)
    loaded, gran = load_candle_csv(path)
    assert gran == HOUR
    assert loaded == candles


def test_fetch_trade_candles_reports_error_without_stopping_others(tmp_path: Path):
    store = make_store(tmp_path)
    opened = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
    closed = opened + timedelta(hours=1)
    ok_trade = _closed_trade(
        store, ticker="SOL", product_id="SOL-USD", opened_at=opened, closed_at=closed
    )
    bad_trade = _closed_trade(
        store, ticker="PUMP", product_id="PUMP-USD", opened_at=opened, closed_at=closed
    )
    _closed_trade(
        store,
        ticker="BTC",
        product_id="BTC-USD",
        opened_at=opened,
        closed_at=closed,
        fingerprint="deadbeef",
    )
    transport = CandleTransport(fail_products={"PUMP-USD"})
    out_dir = tmp_path / "candles"
    with _client(transport) as client:
        results = fetch_trade_candles(
            store,
            out_dir,
            client=client,
            now=FAR_FUTURE,
            sleep=_noop_sleep,
            pause_s=0.0,
        )

    by_ticker = {row.ticker: row for row in results}
    assert set(by_ticker) == {"SOL", "PUMP"}
    assert by_ticker["SOL"].status == "ok"
    assert by_ticker["SOL"].trade_id == ok_trade.id
    assert by_ticker["SOL"].bars >= 2
    assert by_ticker["SOL"].path is not None
    loaded, gran = load_candle_csv(by_ticker["SOL"].path)
    assert gran == legacy_profile("intraday").trail_granularity_seconds
    assert len(loaded) == by_ticker["SOL"].bars
    assert by_ticker["PUMP"].status == "error"
    assert by_ticker["PUMP"].trade_id == bad_trade.id
    assert by_ticker["PUMP"].message
    pump_requests = [req for req in transport.requests if "PUMP-USD" in req.url.path]
    assert len(pump_requests) == 3
    assert not any("authorization" in req.headers for req in transport.requests)


def test_fetch_candles_retries_429_then_succeeds():
    start = 1_700_006_400
    end = start + 3 * HOUR
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        assert "authorization" not in request.headers
        if hits["n"] < 3:
            return httpx.Response(429, json={"message": "slow down"})
        gran = int(request.url.params["granularity"])
        req_start = parse_utc(request.url.params["start"])
        req_end = parse_utc(request.url.params["end"])
        rows = [_row(ts) for ts in range(req_start, req_end + 1, gran)]
        rows.reverse()
        return httpx.Response(200, json=rows)

    transport = httpx.MockTransport(handler)
    with _client(transport) as client:
        candles = fetch_candles(
            client, "BTC-USD", HOUR, start, end, sleep=_noop_sleep, pause_s=0.0
        )
    assert hits["n"] == 3
    assert [c.ts for c in candles] == [start, start + HOUR, start + 2 * HOUR]


def test_fetch_candles_sends_no_authorization_header():
    start = 1_700_006_400
    end = start + 5 * HOUR
    transport = CandleTransport()
    headers = {"User-Agent": "social-momentum-trader", "X-Unused": "1"}
    with httpx.Client(transport=transport, headers=headers) as client:
        fetch_candles(client, "BTC-USD", HOUR, start, end, sleep=_noop_sleep, pause_s=0.0)
    assert transport.requests
    for request in transport.requests:
        names = {name.lower() for name in request.headers}
        assert "authorization" not in names
        assert request.headers.get("authorization") is None
        parsed = urlparse(str(request.url))
        assert parsed.path == "/products/BTC-USD/candles"
        assert parsed.netloc == urlparse(API_BASE).netloc
        qs = parse_qs(parsed.query)
        assert "granularity" in qs
        assert "start" in qs
        assert "end" in qs


def test_fetch_trade_candles_cached_and_gap_fill(tmp_path: Path):
    store = make_store(tmp_path)
    opened = datetime(2026, 1, 3, 8, 0, tzinfo=UTC)
    closed = opened + timedelta(hours=2)
    trade = _closed_trade(
        store, ticker="ETH", product_id="ETH-USD", opened_at=opened, closed_at=closed
    )
    profile = legacy_profile("intraday")
    start, end, gran = trade_candle_window(
        trade, profile, get_market().atr_periods, now=FAR_FUTURE
    )
    skip = {start + gran}
    transport = CandleTransport(skip=skip)
    out_dir = tmp_path / "candles"
    with _client(transport) as client:
        results = fetch_trade_candles(
            store,
            out_dir,
            client=client,
            now=FAR_FUTURE,
            sleep=_noop_sleep,
            pause_s=0.0,
        )
    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].filled == 1
    loaded, loaded_g = load_candle_csv(results[0].path)
    assert loaded_g == gran
    assert loaded[1].ts == start + gran
    assert loaded[1].volume == 0.0
    assert loaded[1].close == loaded[0].close

    n_first = len(transport.requests)
    with _client(transport) as client:
        cached = fetch_trade_candles(
            store,
            out_dir,
            client=client,
            now=FAR_FUTURE,
            sleep=_noop_sleep,
            pause_s=0.0,
        )
    assert cached[0].status == "cached"
    assert cached[0].path == candle_csv_path(out_dir, trade)
    assert len(transport.requests) == n_first

    with _client(transport) as client:
        again = fetch_trade_candles(
            store,
            out_dir,
            client=client,
            now=FAR_FUTURE,
            overwrite=True,
            sleep=_noop_sleep,
            pause_s=0.0,
        )
    assert again[0].status == "ok"
    assert len(transport.requests) > n_first


def test_cli_fetch_candles_flags_and_all_error_exit(tmp_path: Path, monkeypatch, capsys):
    parser = build_parser()
    default = parser.parse_args(["fetch-candles"])
    assert default.out_dir == "data/candles"
    assert default.fingerprint_prefix == GEN8_CONFIG_FINGERPRINT_PREFIX
    assert default.overwrite is False

    args = parser.parse_args(
        [
            "fetch-candles",
            "--out-dir",
            str(tmp_path / "out"),
            "--fingerprint-prefix",
            "abc",
            "--overwrite",
        ]
    )
    assert args.overwrite is True
    assert args.fingerprint_prefix == "abc"

    store = make_store(tmp_path)
    monkeypatch.setattr(
        "smt.config.get_settings",
        lambda: type("S", (), {"database_url": store.database_url})(),
    )
    errors = [
        FetchResult(
            trade_id=1,
            ticker="SOL",
            product_id="SOL-USD",
            granularity=900,
            bars=0,
            filled=0,
            path=None,
            status="error",
            message="boom",
        )
    ]
    monkeypatch.setattr(
        "smt.ops.candle_fetch.fetch_trade_candles",
        lambda *a, **k: errors,
    )
    assert args.func(parser.parse_args(["fetch-candles", "--out-dir", str(tmp_path)])) == 1
    out = capsys.readouterr().out
    assert "error trade=1 SOL SOL-USD" in out
    assert "fetched 0, cached 0, errors 1 ->" in out


def test_swing_snapshot_uses_4h_trail_in_path():
    snapshot = resolve_profile(legacy_profile("swing")).snapshot()
    snapshot["trail_granularity_seconds"] = FOUR_H
    trade = Trade(
        id=9,
        ticker="BTC",
        strategy="swing",
        product_id="BTC-USD",
        qty=1.0,
        entry_price=100.0,
        entry_notional=100.0,
        take_profit=110.0,
        stop_loss=90.0,
        time_stop_at=FAR_FUTURE,
        exit_snapshot=snapshot,
    )
    path = candle_csv_path(Path("data/candles"), trade)
    assert path.name == "trade_9_BTC-USD_14400.csv"


def test_format_fetch_line_ok_and_error():
    ok = FetchResult(
        trade_id=3,
        ticker="SOL",
        product_id="SOL-USD",
        granularity=900,
        bars=40,
        filled=2,
        path=Path("data/candles/trade_3_SOL-USD_900.csv"),
        status="ok",
    )
    line = format_fetch_line(ok)
    assert line.startswith("ok trade=3 SOL SOL-USD")
    assert "bars=40" in line
    err = FetchResult(
        trade_id=4,
        ticker="PUMP",
        product_id="PUMP-USD",
        granularity=900,
        bars=0,
        filled=0,
        path=None,
        status="error",
        message="HTTP 500",
    )
    assert "HTTP 500" in format_fetch_line(err)
