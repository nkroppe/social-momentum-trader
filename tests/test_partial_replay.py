"""Network-free tests for report-only post-partial exit replay."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from _helpers import make_store

from smt.cli import build_parser
from smt.market.indicators import Candle, atr
from smt.models import ExitReason, Trade, TradeStatus
from smt.ops.candle_fetch import candle_csv_path, write_candle_csv
from smt.ops.partial_replay import (
    SKIP_CSV_ERROR,
    SKIP_NO_CSV,
    SKIP_NO_PARTIAL,
    SKIP_STALE_BEFORE_TARGET,
    SKIP_STOPPED_BEFORE_TARGET,
    SKIP_ZERO_RISK,
    find_partial_bar,
    format_partial_replay,
    realized_r_after_partial,
    replay_trade,
    run_partial_replay,
    walk_post_partial,
)
from smt.ops.reports import GEN8_CONFIG_FINGERPRINT_PREFIX, SMALL_N_THRESHOLD, trade_realized_r
from smt.trader.exit_policy import first_partial_quantity, legacy_profile

GEN8_FP = GEN8_CONFIG_FINGERPRINT_PREFIX + "a" * (64 - len(GEN8_CONFIG_FINGERPRINT_PREFIX))
G = 3600
OPENED = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
OPENED_TS = int(OPENED.timestamp())
ENTRY = 100.0
TP = 110.0
SL = 90.0
QTY = 2.0
RISK = 10.0
RATE = 0.006
ATR_PERIODS = 2


def _profile(**overrides):
    return replace(legacy_profile("intraday"), **overrides)


def _bar(offset: int, open_: float, high: float, low: float, close: float) -> Candle:
    return Candle(
        ts=OPENED_TS + offset * G,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=1.0,
    )


def _find(candles: list[Candle], profile=None, **kwargs):
    params = dict(
        candles=candles,
        granularity=G,
        opened_at=OPENED,
        stop_loss=SL,
        take_profit=TP,
        profile=profile or _profile(time_stop_hours=24),
        entry_price=ENTRY,
        initial_risk_per_unit=RISK,
    )
    params.update(kwargs)
    return find_partial_bar(**params)


def _replay(candles: list[Candle], profile=None, **kwargs):
    params = dict(
        opened_at=OPENED,
        entry_price=ENTRY,
        stop_loss=SL,
        take_profit=TP,
        original_qty=QTY,
        initial_risk_per_unit=RISK,
        candles=candles,
        granularity=G,
        profile=profile or _profile(time_stop_hours=24),
        atr_periods=ATR_PERIODS,
        fee_rate=RATE,
    )
    params.update(kwargs)
    return replay_trade(**params)


def _closed_trade(store, *, candles_dir: Path | None = None, candles=None, **overrides) -> Trade:
    opened = overrides.pop("opened_at", OPENED)
    closed = overrides.pop("closed_at", opened + timedelta(hours=2))
    profile = overrides.pop("profile", _profile(trail_granularity_seconds=G, time_stop_hours=24))
    trade = store.add_trade(
        Trade(
            ticker=overrides.pop("ticker", "SOL"),
            strategy=overrides.pop("strategy", "intraday"),
            product_id=overrides.pop("product_id", "SOL-USD"),
            is_live=False,
            status=TradeStatus.CLOSED,
            qty=overrides.pop("qty", 1.0),
            original_qty=overrides.pop("original_qty", QTY),
            entry_price=overrides.pop("entry_price", ENTRY),
            entry_notional=overrides.pop("entry_notional", ENTRY * QTY),
            take_profit=overrides.pop("take_profit", TP),
            stop_loss=overrides.pop("stop_loss", SL),
            initial_risk_per_unit=overrides.pop("initial_risk_per_unit", RISK),
            partial_taken=overrides.pop("partial_taken", True),
            time_stop_at=overrides.pop("time_stop_at", closed),
            exit_price=overrides.pop("exit_price", 105.0),
            exit_reason=overrides.pop("exit_reason", ExitReason.TRAILING_STOP),
            realized_pnl=overrides.pop("realized_pnl", 8.0),
            config_fingerprint=overrides.pop("fingerprint", GEN8_FP),
            exit_snapshot=profile.snapshot(),
            opened_at=opened,
            closed_at=closed,
        )
    )
    if candles_dir is not None and candles is not None:
        write_candle_csv(candle_csv_path(candles_dir, trade), candles)
    return trade


def _be_stop() -> float:
    return max(SL, ENTRY + ENTRY * RATE + ENTRY * RATE)


def test_partial_detected_on_first_bar_whose_high_reaches_tp():
    candles = [
        _bar(-1, 100, 101, 99, 100),
        _bar(0, 100, 105, 99, 104),
        _bar(1, 104, 109, 103, 108),
        _bar(2, 108, 110, 107, 109),
        _bar(3, 109, 111, 101.0, 102),
    ]
    idx = _find(candles)
    assert idx == 3
    assert candles[idx].high >= TP
    assert candles[2].high < TP

    on_entry = [
        _bar(0, 100, 110, 99, 109),
        _bar(1, 109, 111, 101.0, 102),
    ]
    assert _find(on_entry) == 0


def test_stop_before_tp_is_skipped():
    candles = [
        _bar(0, 100, 105, 99, 104),
        _bar(1, 104, 109, 89, 90),
        _bar(2, 90, 112, 90, 111),
    ]
    assert _find(candles) == SKIP_STOPPED_BEFORE_TARGET
    assert _replay(candles) == SKIP_STOPPED_BEFORE_TARGET


def test_bar_touching_stop_and_new_high_exits_at_stop():
    be = _be_stop()
    candles = [
        _bar(0, 100, 110, 99, 109),
        _bar(1, 109, 120, 100, 118),
    ]
    result = _replay(candles)
    assert not isinstance(result, str)
    # Open 109, stop 101.2 -> fill at the stop even though high printed 120.
    assert result.b.price == pytest.approx(be)
    assert result.b.price == pytest.approx(min(candles[1].open, be))
    assert result.b.reason in ("TRAILING_STOP", "STOP_LOSS")
    assert result.partial_ts == candles[0].ts

    walked = walk_post_partial(
        candles,
        G,
        0,
        OPENED,
        24,
        SL,
        TP,
        ENTRY,
        RATE,
        ATR_PERIODS,
        "b",
    )
    assert walked.price == pytest.approx(be)


def test_variant_b_exits_at_be_fees_with_hand_computed_r():
    be = _be_stop()
    assert be == pytest.approx(101.2)
    candles = [
        _bar(0, 100, 110, 100, 109),
        _bar(1, 102, 103, 101.0, 102),
    ]
    result = _replay(candles)
    assert not isinstance(result, str)
    assert result.b.price == pytest.approx(be)
    qty_p = first_partial_quantity(QTY, QTY, 0.5)
    remaining = QTY - qty_p
    fees = RATE * (ENTRY * QTY + TP * qty_p + be * remaining)
    net = (TP - ENTRY) * qty_p + (be - ENTRY) * remaining - fees
    expected_r = net / (RISK * QTY)
    assert expected_r == pytest.approx(0.43664)
    assert result.b_r == pytest.approx(expected_r)
    assert result.b_r == pytest.approx(
        realized_r_after_partial(
            entry=ENTRY,
            take_profit=TP,
            exit_price=be,
            qty_p=qty_p,
            remaining=remaining,
            original_qty=QTY,
            fee_rate=RATE,
            initial_risk_per_unit=RISK,
        )
    )


def test_variant_c_trails_two_atr_and_exits_on_ratcheted_stop():
    tp = 104.0
    candles = [
        _bar(-2, 100, 102, 100, 100),
        _bar(-1, 100, 102, 100, 100),
        _bar(0, 100, 102, 100, 102),
        _bar(1, 102, 104, 102, 104),
        _bar(2, 104, 106, 104, 106),
        _bar(3, 106, 106, 101.5, 102),
    ]
    result = _replay(candles, take_profit=tp)
    assert not isinstance(result, str)
    assert result.partial_ts == candles[3].ts
    highest0 = max(tp, candles[3].high)
    atr0 = atr(candles[:4], ATR_PERIODS)
    stop0 = max(SL, highest0 - 2.0 * atr0)
    assert candles[4].low > stop0
    highest1 = max(highest0, candles[4].high)
    atr1 = atr(candles[:5], ATR_PERIODS)
    stop1 = max(stop0, SL, highest1 - 2.0 * atr1)
    assert candles[5].low <= stop1
    assert result.c.price == pytest.approx(min(candles[5].open, stop1))
    assert result.c.reason in ("TRAILING_STOP", "STOP_LOSS")
    assert stop1 > stop0


def test_gap_open_below_stop_fills_at_open():
    be = _be_stop()
    candles = [
        _bar(0, 100, 110, 100, 109),
        _bar(1, 100, 101, 99, 100),
    ]
    result = _replay(candles)
    assert not isinstance(result, str)
    assert be > 100
    assert result.b.price == pytest.approx(100.0)
    assert result.b.price == pytest.approx(min(candles[1].open, be))


def test_time_stop_exits_at_bar_close():
    candles = [
        _bar(-2, 100, 105, 100, 100),
        _bar(-1, 100, 105, 100, 100),
        _bar(0, 100, 110, 104, 109),
        _bar(1, 109, 111, 108, 110),
        _bar(2, 110, 112, 108.5, 108.5),
    ]
    result = _replay(candles, profile=_profile(time_stop_hours=3))
    assert not isinstance(result, str)
    assert result.b.reason == "TIME_STOP"
    assert result.b.price == pytest.approx(108.5)
    assert result.b.data_end is False
    assert result.c.reason == "TIME_STOP"
    assert result.c.price == pytest.approx(108.5)


def test_data_end_flag_when_csv_ends_first():
    candles = [
        _bar(-2, 100, 105, 100, 100),
        _bar(-1, 100, 105, 100, 100),
        _bar(0, 100, 110, 104, 109),
        _bar(1, 109, 111, 108, 110.5),
    ]
    result = _replay(candles, profile=_profile(time_stop_hours=24))
    assert not isinstance(result, str)
    assert result.b.reason == "DATA_END"
    assert result.b.data_end is True
    assert result.b.price == pytest.approx(110.5)
    assert result.c.reason == "DATA_END"


def test_entry_bar_low_below_stop_is_ignored():
    candles = [
        _bar(0, 100, 105, 50, 104),
        _bar(1, 104, 110, 103, 109),
        _bar(2, 102, 103, 101.0, 102),
    ]
    found = _find(candles)
    assert found == 1
    result = _replay(candles)
    assert not isinstance(result, str)
    assert result.partial_ts == candles[1].ts


def test_missing_csv_is_skipped(tmp_path: Path):
    store = make_store(tmp_path)
    trade = _closed_trade(store)
    result = run_partial_replay(
        store,
        tmp_path / "candles",
        atr_periods=ATR_PERIODS,
        fee_rates={"intraday": RATE},
    )
    assert result.n == 0
    assert result.rows == ()
    assert len(result.skipped) == 1
    assert result.skipped[0].trade_id == trade.id
    assert result.skipped[0].reason == SKIP_NO_CSV
    text = format_partial_replay(result)
    assert "no csv" in text
    assert "n=0 [small-n]" in text


def test_means_and_small_n_flag(tmp_path: Path):
    store = make_store(tmp_path)
    candles_dir = tmp_path / "candles"
    be = _be_stop()
    candles = [
        _bar(-1, 100, 101, 99, 100),
        _bar(0, 100, 110, 100, 109),
        _bar(1, 102, 103, 101.0, 102),
    ]
    t1 = _closed_trade(
        store, candles_dir=candles_dir, candles=candles, realized_pnl=10.0, ticker="SOL"
    )
    t2 = _closed_trade(
        store, candles_dir=candles_dir, candles=candles, realized_pnl=4.0, ticker="ETH"
    )
    result = run_partial_replay(
        store,
        candles_dir,
        atr_periods=ATR_PERIODS,
        fee_rates={"intraday": RATE},
    )
    assert result.n == 2
    assert result.n < SMALL_N_THRESHOLD
    assert result.small_n is True
    assert result.mean_actual_r == pytest.approx(
        (trade_realized_r(t1) + trade_realized_r(t2)) / 2
    )
    qty_p = first_partial_quantity(QTY, QTY, 0.5)
    expected_b = realized_r_after_partial(
        entry=ENTRY,
        take_profit=TP,
        exit_price=be,
        qty_p=qty_p,
        remaining=QTY - qty_p,
        original_qty=QTY,
        fee_rate=RATE,
        initial_risk_per_unit=RISK,
    )
    assert result.mean_b_r == pytest.approx(expected_b)
    text = format_partial_replay(result)
    assert "Candle granularity" in text
    assert "Stop-first ambiguity" in text
    assert "Stale time stop before target" in text
    assert "Variant b" in text
    assert "Variant c" in text
    assert "Fee rate" in text
    assert "MEAN" in text
    assert "n=2 [small-n]" in text
    assert "SOL" in text
    assert "ETH" in text


def test_zero_risk_and_csv_error_skipped(tmp_path: Path):
    store = make_store(tmp_path)
    candles_dir = tmp_path / "candles"
    zero = _closed_trade(store, initial_risk_per_unit=0.0, ticker="BTC")
    bad = _closed_trade(store, ticker="PUMP")
    path = candle_csv_path(candles_dir, bad)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not,a,candle,file\n", encoding="utf-8")
    result = run_partial_replay(
        store,
        candles_dir,
        atr_periods=ATR_PERIODS,
        fee_rates={"intraday": RATE},
    )
    reasons = {row.trade_id: row.reason for row in result.skipped}
    assert reasons[zero.id] == SKIP_ZERO_RISK
    assert reasons[bad.id] == SKIP_CSV_ERROR
    text = format_partial_replay(result)
    assert "zero risk=1" in text
    assert "csv error=1" in text


def test_no_partial_in_window_skipped():
    candles = [
        _bar(0, 100, 105, 99, 104),
        _bar(1, 104, 106, 103, 105),
        _bar(2, 105, 107, 104, 106),
    ]
    assert _replay(candles) == SKIP_NO_PARTIAL


def test_stale_time_stop_before_later_tp_is_skipped():
    profile = _profile(
        time_stop_hours=24,
        stale_time_stop_hours=2,
        stale_mfe_r=1.0,
        advanced_exit_enabled=True,
    )
    candles = [
        _bar(0, 100, 105, 99, 104),
        _bar(1, 104, 106, 103, 105),
        _bar(2, 105, 110, 104, 109),
        _bar(3, 109, 111, 101.0, 102),
    ]
    assert candles[2].high >= TP
    assert _find(candles, profile=profile) == SKIP_STALE_BEFORE_TARGET
    assert _replay(candles, profile=profile) == SKIP_STALE_BEFORE_TARGET


def test_tp_before_stale_time_is_still_a_partial():
    profile = _profile(
        time_stop_hours=24,
        stale_time_stop_hours=4,
        stale_mfe_r=1.0,
        advanced_exit_enabled=True,
    )
    candles = [
        _bar(0, 100, 105, 99, 104),
        _bar(1, 104, 110, 103, 109),
        _bar(2, 102, 103, 101.0, 102),
    ]
    found = _find(candles, profile=profile)
    assert found == 1
    result = _replay(candles, profile=profile)
    assert not isinstance(result, str)
    assert result.partial_ts == candles[1].ts


def test_cli_partial_replay_end_to_end(tmp_path: Path, monkeypatch, capsys):
    parser = build_parser()
    default = parser.parse_args(["partial-replay"])
    assert default.candles_dir == "data/candles"
    assert default.fingerprint_prefix == GEN8_CONFIG_FINGERPRINT_PREFIX

    store = make_store(tmp_path)
    candles_dir = tmp_path / "candles"
    candles = [
        _bar(-1, 100, 101, 99, 100),
        _bar(0, 100, 110, 100, 109),
        _bar(1, 102, 103, 101.0, 102),
    ]
    trade = _closed_trade(store, candles_dir=candles_dir, candles=candles, realized_pnl=6.0)
    monkeypatch.setattr(
        "smt.config.get_settings",
        lambda: type("S", (), {"database_url": store.database_url})(),
    )
    args = parser.parse_args(
        [
            "partial-replay",
            "--candles-dir",
            str(candles_dir),
            "--fingerprint-prefix",
            GEN8_CONFIG_FINGERPRINT_PREFIX,
        ]
    )
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "SOL" in out
    assert "MEAN" in out
    assert "n=1 [small-n]" in out
    assert "Stop-first ambiguity" in out
    assert str(trade.id) in out
    assert "TRAILING_STOP" in out or "STOP_LOSS" in out or "DATA_END" in out
