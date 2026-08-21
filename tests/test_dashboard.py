"""Dashboard API: auth, overview, positions, and trade filters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from _helpers import make_store
from fastapi.testclient import TestClient

from smt.config import Settings
from smt.dashboard.app import create_app
from smt.models import (
    ExitReason,
    RiskEquitySnapshot,
    Trade,
    TradeStatus,
    utcnow,
)
from smt.store import opportunity_key


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite:///{tmp_path}/dash.sqlite",
        "paper_start_equity": 10_000.0,
        "kill_file": str(tmp_path / "KILL"),
        "dashboard_token": "secret-token",
        "live": False,
    }
    values.update(overrides)
    return Settings(**values)


def _open_trade(store, *, ticker="SOL", strategy="intraday", entry=100.0, qty=2.0):
    now = utcnow()
    return store.add_trade(
        Trade(
            ticker=ticker,
            strategy=strategy,
            product_id=f"{ticker}-USD",
            is_live=False,
            status=TradeStatus.OPEN,
            qty=qty,
            original_qty=qty,
            entry_price=entry,
            entry_notional=entry * qty,
            take_profit=entry * 1.06,
            stop_loss=entry * 0.97,
            trailing_stop=entry * 0.97,
            time_stop_at=now + timedelta(hours=6),
            opened_at=now,
        )
    )


def _closed_trade(
    store, *, ticker="SOL", strategy="intraday", pnl=25.0, reason=ExitReason.TAKE_PROFIT
):
    closed_at = datetime(2026, 8, 11, 15, tzinfo=UTC)
    return store.add_trade(
        Trade(
            ticker=ticker,
            strategy=strategy,
            product_id=f"{ticker}-USD",
            is_live=False,
            status=TradeStatus.CLOSED,
            qty=1.0,
            original_qty=1.0,
            entry_price=100.0,
            entry_notional=100.0,
            take_profit=110.0,
            stop_loss=95.0,
            time_stop_at=closed_at,
            exit_price=100.0 + pnl,
            exit_reason=reason,
            realized_pnl=pnl,
            fees_paid=1.25,
            opened_at=closed_at - timedelta(hours=3),
            closed_at=closed_at,
        )
    )


def _client(tmp_path, store, *, token="secret-token", require_auth=True, marks=None):
    settings = _settings(tmp_path, dashboard_token=token)
    app = create_app(
        store=store,
        settings=settings,
        token=token,
        require_auth=require_auth,
        marks=marks or {"SOL-USD": 110.0, "BTC-USD": 70_000.0},
    )
    return TestClient(app)


def test_healthz_is_public(tmp_path):
    store = make_store(tmp_path)
    client = _client(tmp_path, store)
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"ok": True}


def test_api_rejects_missing_and_wrong_token(tmp_path):
    store = make_store(tmp_path)
    client = _client(tmp_path, store)
    assert client.get("/api/overview").status_code == 401
    assert client.get("/api/overview", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_overview_and_positions_use_injected_marks(tmp_path):
    store = make_store(tmp_path)
    _open_trade(store)
    _closed_trade(store, pnl=40.0)
    client = _client(tmp_path, store, marks={"SOL-USD": 110.0})
    headers = {"Authorization": "Bearer secret-token"}

    overview = client.get("/api/overview", headers=headers)
    assert overview.status_code == 200
    body = overview.json()
    assert body["mode"] == "PAPER"
    assert body["open_positions"] == 1
    assert body["closed_trades"] == 1
    assert body["realized_pnl"] == 40.0
    # MTM = (110-100)*2 = 20; equity = 10000 + 40 + 20
    assert body["unrealized_pnl"] == 20.0
    assert abs(body["equity"] - 10_060.0) < 1e-9

    positions = client.get("/api/positions", headers=headers)
    assert positions.status_code == 200
    row = positions.json()["positions"][0]
    assert row["ticker"] == "SOL"
    assert row["mark"] == 110.0
    assert row["mark_ok"] is True
    assert row["unrealized_pnl"] == 20.0
    assert row["exit_profile_label"] == "legacy"
    assert row["config_fingerprint"] == ""
    assert row["exit_snapshot"] is None
    assert row["mfe_r"] == 0.0
    assert row["hold_hours"] >= 0.0


def test_trades_filter_by_strategy_and_exit_reason(tmp_path):
    store = make_store(tmp_path)
    _closed_trade(store, strategy="intraday", pnl=10.0, reason=ExitReason.TAKE_PROFIT)
    _closed_trade(store, ticker="BTC", strategy="swing", pnl=-8.0, reason=ExitReason.STOP_LOSS)
    client = _client(tmp_path, store)
    headers = {"Authorization": "Bearer secret-token"}

    all_rows = client.get("/api/trades", headers=headers).json()
    assert all_rows["total"] == 2

    swing = client.get("/api/trades", params={"strategy": "swing"}, headers=headers).json()
    assert swing["total"] == 1
    assert swing["trades"][0]["ticker"] == "BTC"

    stops = client.get("/api/trades", params={"exit_reason": "STOP_LOSS"}, headers=headers).json()
    assert stops["total"] == 1
    assert stops["trades"][0]["realized_pnl"] == -8.0
    assert stops["trades"][0]["exit_profile_label"] == "legacy"
    assert stops["trades"][0]["exit_snapshot"] is None
    assert stops["trades"][0]["mfe_r"] == 0.0


def test_performance_and_risk_and_opportunities(tmp_path):
    store = make_store(tmp_path)
    _open_trade(store, ticker="SOL", qty=1.0, entry=100.0)
    _closed_trade(store, pnl=12.0)
    now = utcnow()
    with store.session() as session:
        session.add(
            RiskEquitySnapshot(
                strategy="intraday",
                period="day",
                bucket_start=now.replace(hour=0, minute=0, second=0, microsecond=0),
                equity=10_000.0,
            )
        )
        session.commit()
    key = opportunity_key(
        config_fingerprint="a" * 64,
        run_id="test",
        strategy="intraday",
        ticker="SOL",
        trigger_candle_ts=1_700_000_000,
    )
    store.upsert_opportunity(
        opportunity_key=key,
        ledger_version=1,
        config_fingerprint="a" * 64,
        run_id="test",
        strategy="intraday",
        ticker="SOL",
        product_id="SOL-USD",
        trigger_granularity_seconds=900,
        trigger_candle_ts=1_700_000_000,
        trigger_closed_at=now,
        outcome_status="opened",
        outcome_reason="filled",
        setup_name="breakout",
        evaluated_at=now,
    )
    store.upsert_shadow_decision(
        decision_key="shadow-1",
        ticker="SOL",
        strategy="intraday",
        social_decision="confirm",
        llm_status="complete",
        llm_veto=False,
        first_evaluated_at=now,
    )

    client = _client(tmp_path, store, marks={"SOL-USD": 105.0})
    headers = {"Authorization": "Bearer secret-token"}

    perf = client.get("/api/performance", headers=headers)
    assert perf.status_code == 200
    names = {row["strategy"] for row in perf.json()["strategies"]}
    assert "intraday" in names
    assert any(row["reason"] == "TAKE_PROFIT" for row in perf.json()["exit_reasons"])

    risk = client.get("/api/risk", headers=headers).json()
    assert risk["open_positions"] == 1
    assert risk["gross_exposure"] == 105.0
    naive_heat = max(105.0 - 97.0, 0.0) * 1.0
    assert risk["open_heat"] > naive_heat
    assert risk["snapshots"]

    opps = client.get("/api/opportunities", headers=headers).json()
    assert opps["funnel"].get("opened") == 1
    assert opps["rows"][0]["ticker"] == "SOL"

    shadow = client.get("/api/shadow", headers=headers).json()
    assert shadow["total"] == 1
    assert shadow["social_counts"]["confirm"] == 1


def test_loopback_can_skip_auth(tmp_path):
    store = make_store(tmp_path)
    client = _client(tmp_path, store, token="", require_auth=False)
    assert client.get("/api/health").status_code == 200


def test_shadow_summary_is_not_capped_by_page_size(tmp_path):
    store = make_store(tmp_path)
    now = utcnow()
    for i in range(3):
        store.upsert_shadow_decision(
            decision_key=f"shadow-{i}",
            ticker="SOL",
            strategy="intraday",
            social_decision="confirm" if i < 2 else "veto",
            llm_status="complete",
            llm_veto=i == 2,
            first_evaluated_at=now,
        )
    client = _client(tmp_path, store)
    headers = {"Authorization": "Bearer secret-token"}
    shadow = client.get("/api/shadow", params={"limit": 1}, headers=headers).json()
    assert len(shadow["rows"]) == 1
    assert shadow["total"] == 3
    assert shadow["llm_veto_count"] == 1
    assert shadow["social_counts"]["confirm"] == 2
    assert shadow["social_counts"]["veto"] == 1


def test_live_equity_uses_exchange_reader(tmp_path):
    store = make_store(tmp_path)
    _open_trade(store)
    settings = _settings(tmp_path, live=True)
    from smt.dashboard.service import DashboardService

    service = DashboardService(
        store,
        settings,
        marks={"SOL-USD": 110.0},
        live_equity=lambda: 12_345.0,
    )
    app = create_app(
        store=store,
        settings=settings,
        token="secret-token",
        require_auth=True,
        service=service,
    )
    body = (
        TestClient(app)
        .get("/api/overview", headers={"Authorization": "Bearer secret-token"})
        .json()
    )
    assert body["mode"] == "LIVE"
    assert body["equity"] == 12_345.0
    assert body["unrealized_pnl"] == 20.0


def test_live_equity_falls_back_when_reader_fails(tmp_path):
    store = make_store(tmp_path)
    _closed_trade(store, pnl=40.0)
    settings = _settings(tmp_path, live=True)
    from smt.dashboard.service import DashboardService

    def boom() -> float:
        raise RuntimeError("coinbase down")

    service = DashboardService(store, settings, marks={}, live_equity=boom)
    app = create_app(
        store=store,
        settings=settings,
        token="secret-token",
        require_auth=True,
        service=service,
    )
    body = (
        TestClient(app)
        .get("/api/overview", headers={"Authorization": "Bearer secret-token"})
        .json()
    )
    assert body["equity"] == 10_040.0
