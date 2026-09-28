"""Go-live edge gate: closed-trade net P/L, preflight check, live latches."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from _helpers import make_store

from smt.config import LIVE_ACK_PHRASE, Settings, get_ops, get_security
from smt.models import Trade, TradeStatus
from smt.ops.edge_gate import evaluate_edge_gate, live_edge_gate
from smt.ops.preflight import run_preflight
from smt.policy import trading_policy_identity
from smt.run import Runner

FP = "c95a0ad410f4" + "a" * 52
OTHER_FP = "c95a0ad410f4" + "b" * 52
PREFIX_FP = "c95a0ad410f4"


def _trade(
    *,
    fingerprint: str,
    pnl: float,
    status: TradeStatus = TradeStatus.CLOSED,
    ticker: str = "SOL",
) -> Trade:
    now = datetime(2026, 8, 16, 20, tzinfo=UTC)
    return Trade(
        ticker=ticker,
        product_id=f"{ticker}-USD",
        qty=1.0,
        original_qty=1.0,
        entry_price=100.0,
        entry_notional=250.0,
        take_profit=110.0,
        stop_loss=95.0,
        time_stop_at=now,
        status=status,
        realized_pnl=pnl,
        config_fingerprint=fingerprint,
        opened_at=now - timedelta(hours=3),
        closed_at=now if status == TradeStatus.CLOSED else None,
    )


def _write_ready_soak(path, fingerprint: str) -> None:
    started = datetime.now(UTC) - timedelta(days=30)
    path.write_text(
        json.dumps(
            {
                "started_at": started.isoformat(),
                "mode": "paper",
                "active_fingerprint": fingerprint,
                "manifest": {},
                "generation": 1,
            }
        ),
        encoding="utf-8",
    )


def _latch_runner(tmp_path, *, live: bool, live_ack: str = "", fingerprint: str = FP, store=None):
    runner = Runner.__new__(Runner)
    db_url = store.database_url if store is not None else f"sqlite:///{tmp_path}/t.sqlite"
    runner.settings = Settings(live=live, live_ack=live_ack, database_url=db_url)
    runner.security = get_security()
    ops = get_ops().model_copy(deep=True)
    ops.soak.state_file = str(tmp_path / "soak.json")
    runner.ops = ops
    runner.config_fingerprint = fingerprint
    return runner


def test_negative_net_fails():
    result = evaluate_edge_gate(
        [_trade(fingerprint=FP, pnl=-40.0), _trade(fingerprint=FP, pnl=-12.47)],
        FP,
    )
    assert result.passed is False
    assert result.n == 2
    assert result.net_pnl == -52.47
    assert result.fingerprint == FP
    assert result.detail == "net $-52.47 over n=2 closed trades on fp c95a0ad410f4 (need > $0)"


def test_zero_net_fails():
    result = evaluate_edge_gate(
        [_trade(fingerprint=FP, pnl=10.0), _trade(fingerprint=FP, pnl=-10.0)],
        FP,
    )
    assert result.passed is False
    assert result.n == 2
    assert result.net_pnl == 0.0
    assert "net $0.00" in result.detail
    assert "n=2" in result.detail


def test_positive_net_passes():
    result = evaluate_edge_gate(
        [_trade(fingerprint=FP, pnl=12.5), _trade(fingerprint=FP, pnl=3.25, ticker="BTC")],
        FP,
    )
    assert result.passed is True
    assert result.n == 2
    assert result.net_pnl == 15.75
    assert result.detail == "net $15.75 over n=2 closed trades on fp c95a0ad410f4 (need > $0)"


def test_n_zero_fails():
    result = evaluate_edge_gate([], FP)
    assert result.passed is False
    assert result.n == 0
    assert result.net_pnl == 0.0
    assert "n=0" in result.detail
    assert FP[:12] in result.detail


def test_empty_fingerprint_fails_without_counting_trades():
    result = evaluate_edge_gate([_trade(fingerprint="", pnl=50.0)], "")
    assert result.passed is False
    assert result.n == 0
    assert result.net_pnl == 0.0
    assert result.fingerprint == ""


def test_other_fingerprints_and_shared_prefix_are_excluded():
    trades = [
        _trade(fingerprint=FP, pnl=5.0),
        _trade(fingerprint=OTHER_FP, pnl=100.0, ticker="BTC"),
        _trade(fingerprint=PREFIX_FP, pnl=100.0, ticker="ETH"),
    ]
    result = evaluate_edge_gate(trades, FP)
    assert result.passed is True
    assert result.n == 1
    assert result.net_pnl == 5.0

    prefix_result = evaluate_edge_gate(trades, PREFIX_FP)
    assert prefix_result.n == 1
    assert prefix_result.net_pnl == 100.0
    assert prefix_result.fingerprint == PREFIX_FP


def test_open_trades_are_excluded():
    trades = [
        _trade(fingerprint=FP, pnl=20.0, status=TradeStatus.OPEN),
        _trade(fingerprint=FP, pnl=-1.0, ticker="BTC"),
    ]
    result = evaluate_edge_gate(trades, FP)
    assert result.passed is False
    assert result.n == 1
    assert result.net_pnl == -1.0


def test_live_edge_gate_reads_closed_trades_from_store(tmp_path):
    store = make_store(tmp_path)
    store.add_trade(_trade(fingerprint=FP, pnl=8.0))
    store.add_trade(_trade(fingerprint=FP, pnl=2.0, ticker="BTC"))
    store.add_trade(_trade(fingerprint=OTHER_FP, pnl=50.0, ticker="ETH"))
    store.add_trade(_trade(fingerprint=FP, pnl=99.0, status=TradeStatus.OPEN, ticker="HYPE"))

    result = live_edge_gate(store, FP)
    assert result.passed is True
    assert result.n == 2
    assert result.net_pnl == 10.0
    assert FP[:12] in result.detail


def test_preflight_live_includes_edge_gate_detail(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("smt.ops.preflight._market_data_checks", lambda: [])
    store = make_store(tmp_path)
    fingerprint = trading_policy_identity().fingerprint
    store.add_trade(_trade(fingerprint=fingerprint, pnl=-4.5))
    store.add_trade(_trade(fingerprint=fingerprint, pnl=-1.25, ticker="BTC"))
    monkeypatch.setattr(
        "smt.ops.preflight.get_settings",
        lambda: Settings(database_url=store.database_url),
    )

    results = {row.name: row for row in run_preflight("live")}
    gate = results["live_edge_gate"]
    assert gate.passed is False
    assert "net $-5.75" in gate.detail
    assert "n=2" in gate.detail
    assert fingerprint[:12] in gate.detail


def test_live_startup_refused_when_edge_gate_fails(tmp_path):
    store = make_store(tmp_path)
    store.add_trade(_trade(fingerprint=FP, pnl=-3.0))
    _write_ready_soak(tmp_path / "soak.json", FP)
    runner = _latch_runner(
        tmp_path,
        live=True,
        live_ack=LIVE_ACK_PHRASE,
        fingerprint=FP,
        store=store,
    )

    runner._enforce_live_latches()

    assert runner.settings.live is False


def test_live_startup_stays_live_when_edge_gate_passes(tmp_path):
    store = make_store(tmp_path)
    store.add_trade(_trade(fingerprint=FP, pnl=7.5))
    _write_ready_soak(tmp_path / "soak.json", FP)
    runner = _latch_runner(
        tmp_path,
        live=True,
        live_ack=LIVE_ACK_PHRASE,
        fingerprint=FP,
        store=store,
    )

    runner._enforce_live_latches()

    assert runner.settings.live is True


def test_paper_startup_skips_edge_gate(tmp_path, monkeypatch):
    calls: list[object] = []

    def _unexpected(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("live_edge_gate must not run on PAPER startup")

    monkeypatch.setattr("smt.run.live_edge_gate", _unexpected)
    runner = _latch_runner(tmp_path, live=False, live_ack=LIVE_ACK_PHRASE, fingerprint=FP)

    runner._enforce_live_latches()

    assert runner.settings.live is False
    assert calls == []
