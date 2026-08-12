"""Network-free deterministic replay and after-cost artifact tests."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest
from _helpers import make_strategy

from smt.backtest import ARTIFACTS, BacktestDataError, load_candle_csv, run_backtest
from smt.config import (
    EntryRulesConfig,
    MarketConfig,
    RiskConfig,
    SignalsConfig,
    TierConfig,
    UniverseConfig,
)
from smt.trader.signals import PriceSetup


def _write_candles(
    path: Path,
    *,
    count: int = 80,
    gap_at: int | None = None,
    ambiguous_at: int | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(("timestamp", "open", "high", "low", "close", "volume"))
        for index in range(count):
            if index == gap_at:
                continue
            ts = index * 3600
            open_ = 100.0 + index * 0.1
            close = open_ + 0.05
            low = open_ - 0.05
            high = close + 0.05
            if index == ambiguous_at:
                low = 90.0
                high = 120.0
            writer.writerow((ts, open_, high, low, close, 100.0))


def _inputs() -> tuple[
    UniverseConfig,
    list,
    MarketConfig,
    RiskConfig,
    SignalsConfig,
]:
    universe = UniverseConfig(symbols={"BTC": {"product_id": "BTC-USD", "tier": "major"}})
    strategy = make_strategy(
        allocation=1.0,
        advanced_exit_enabled=False,
        exit_style="fixed",
        entry=EntryRulesConfig(
            trigger_granularity_seconds=3600,
            bias_granularity_seconds=3600,
            require_compression=False,
            require_trigger_ema_stack=False,
            require_bias_ema_stack=False,
            rsi_min=0,
        ),
    )
    market = MarketConfig()
    market.regime.enabled = False
    market.confirmation.enabled = False
    market.sizing.enabled = False
    signals = SignalsConfig(
        tiers={
            "major": TierConfig(
                social_policy="ignored",
                min_relative_volume=1.0,
                retest_policy="preferred",
            )
        }
    )
    return universe, [strategy], market, RiskConfig(), signals


def _always_setup(trigger, _bias, _rules, _tier, _name, benchmark=None):
    close = trigger[-1].close
    return PriceSetup(
        name="breakout_close",
        entry_price=close,
        structure_stop=close * 0.98,
        stop_pct=0.02,
        atr_pct=0.01,
        conviction=1.0,
        metadata={"trigger_ts": str(trigger[-1].ts)},
    )


def _uneconomic_setup(trigger, _bias, _rules, _tier, _name, benchmark=None):
    close = trigger[-1].close
    return PriceSetup(
        name="breakout_close",
        entry_price=close,
        structure_stop=close * 0.999,
        stop_pct=0.001,
        atr_pct=0.01,
        conviction=1.0,
        metadata={"trigger_ts": str(trigger[-1].ts)},
    )


def test_local_csv_rejects_malformed_and_gapped_input(tmp_path):
    malformed = tmp_path / "bad.csv"
    malformed.write_text("timestamp,open\n0,100\n", encoding="utf-8")
    with pytest.raises(BacktestDataError, match="header"):
        load_candle_csv(malformed)

    gapped = tmp_path / "gapped.csv"
    _write_candles(gapped, gap_at=4)
    with pytest.raises(BacktestDataError, match="gap"):
        load_candle_csv(gapped)


def test_next_bar_entry_stop_first_cost_reconciliation_and_benchmarks(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    _write_candles(data_dir / "BTC-USD.csv", ambiguous_at=52)
    monkeypatch.setattr("smt.backtest.detect_price_setup", _always_setup)
    universe, strategies, market, risk, signals = _inputs()

    result = run_backtest(
        data_dir,
        tmp_path / "out",
        start=str(51 * 3600),
        end=str(60 * 3600),
        symbols=["BTC"],
        universe=universe,
        strategies=strategies,
        market=market,
        risk=risk,
        signals=signals,
        initial_equity=5_000,
    )

    opened = next(row for row in result.opportunities if row["status"] == "opened")
    trade = result.trades[0]
    assert opened["entry_time"] == "1970-01-03T04:00:00Z"
    assert trade["entry_reference"] == pytest.approx(105.2)
    assert trade["entry_price"] == pytest.approx(105.2 * 1.0005)
    assert trade["exit_reason"] == "STOP_LOSS"
    assert trade["net_pnl"] == pytest.approx(
        trade["gross_pnl"] - trade["fees"] - trade["modeled_slippage"]
    )
    assert result.summary["final_equity"] == pytest.approx(
        result.summary["initial_equity"] + result.summary["net_pnl"]
    )
    assert {
        "simple_breakout",
        "btc_buy_hold",
        "equal_weight_buy_hold",
    } <= result.summary["benchmarks"].keys()


def test_backtest_rejects_first_partial_inside_modeled_costs(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    _write_candles(data_dir / "BTC-USD.csv")
    monkeypatch.setattr("smt.backtest.detect_price_setup", _uneconomic_setup)
    universe, strategies, market, risk, signals = _inputs()

    result = run_backtest(
        data_dir,
        tmp_path / "out",
        start=str(51 * 3600),
        end=str(60 * 3600),
        symbols=["BTC"],
        universe=universe,
        strategies=strategies,
        market=market,
        risk=risk,
        signals=signals,
        initial_equity=5_000,
    )

    assert result.trades == []
    assert any(
        row["status"] == "risk_rejected" and "not positively economic" in row["reason"]
        for row in result.opportunities
    )


def test_repeated_runs_are_byte_identical(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    _write_candles(data_dir / "BTC-USD.csv")
    monkeypatch.setattr("smt.backtest.detect_price_setup", _always_setup)
    universe, strategies, market, risk, signals = _inputs()
    kwargs = {
        "start": str(51 * 3600),
        "end": str(65 * 3600),
        "symbols": ["BTC"],
        "universe": universe,
        "strategies": strategies,
        "market": market,
        "risk": risk,
        "signals": signals,
        "initial_equity": 5_000,
    }

    run_backtest(data_dir, tmp_path / "one", **kwargs)
    run_backtest(data_dir, tmp_path / "two", **kwargs)

    for name in ARTIFACTS:
        assert (tmp_path / "one" / name).read_bytes() == (tmp_path / "two" / name).read_bytes()
