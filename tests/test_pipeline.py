"""Smoke tests for the core pipeline: extraction, scoring, risk gate, exits."""

from __future__ import annotations

import uuid
from datetime import timedelta

from smt.config import RiskConfig, UniverseConfig
from smt.ingest.base import extract_tickers
from smt.models import SocialEvent, TradeStatus, utcnow
from smt.scorer import MomentumScorer
from smt.store import Store
from smt.trader.paper import PaperBroker
from smt.trader.risk import RiskGate
from smt.trader.signals import SignalEngine, TradeCandidate


def _universe() -> UniverseConfig:
    return UniverseConfig(
        quote_currency="USD",
        symbols={
            "SOL": {"product_id": "SOL-USD", "aliases": ["sol", "solana", "$sol"]},
            "BTC": {"product_id": "BTC-USD", "aliases": ["btc", "bitcoin"]},
        },
    )


def _store(tmp_path) -> Store:
    s = Store(f"sqlite:///{tmp_path}/t.sqlite")
    s.init_db()
    return s


def test_extract_tickers():
    u = _universe()
    found = extract_tickers("I love $SOL and Bitcoin but not xyz", u)
    assert found == {"SOL", "BTC"}
    # substring guard: "solar" should not match "sol"
    assert extract_tickers("solar panels", u) == set()


def test_velocity_signal_fires_on_burst(tmp_path):
    store = _store(tmp_path)
    u = _universe()
    risk = RiskConfig()

    # Baseline in older buckets.
    for i in range(risk.scorer_lookback_buckets, 1, -1):
        ts = utcnow() - timedelta(minutes=risk.scorer_bucket_minutes * i - 1)
        store.add_events(
            [
                SocialEvent(
                    source="reddit",
                    external_id=uuid.uuid4().hex,
                    ticker="SOL",
                    text="sol",
                    created_at=ts,
                    weight=1.0,
                )
            ]
        )
    # Burst now across two sources.
    for src in ("reddit", "youtube"):
        store.add_events(
            [
                SocialEvent(
                    source=src,
                    external_id=uuid.uuid4().hex,
                    ticker="SOL",
                    text="sol",
                    created_at=utcnow(),
                    weight=1.0,
                )
                for _ in range(12)
            ]
        )

    scorer = MomentumScorer(store, u, risk.scorer_bucket_minutes, risk.scorer_lookback_buckets)
    result = scorer.score_ticker("SOL")
    assert result.zscore >= risk.signal_min_zscore
    assert result.distinct_sources >= 2

    engine = SignalEngine(risk, u)
    cands = engine.candidates(scorer.score_all())
    assert any(c.ticker == "SOL" for c in cands)


def test_risk_gate_blocks_over_limits(tmp_path):
    store = _store(tmp_path)
    risk = RiskConfig(max_open_positions=0)
    gate = RiskGate(risk, store)
    cand = TradeCandidate("SOL", "SOL-USD", 5.0, 20, 2, "x")
    decision = gate.evaluate(cand, equity=5000, start_equity=5000)
    assert not decision.approved


def test_paper_take_profit_closes(tmp_path):
    from smt.config import Settings
    from smt.trader.manager import TradeManager

    store = _store(tmp_path)
    u = _universe()
    risk = RiskConfig()
    broker = PaperBroker(seed=1)
    settings = Settings(paper_start_equity=5000)
    mgr = TradeManager(settings, risk, u, store, broker)

    cand = TradeCandidate("SOL", "SOL-USD", 5.0, 20, 2, "x")
    trade = mgr.open_position(cand, notional_usd=500)
    assert trade.status == TradeStatus.OPEN

    broker.set_price("SOL-USD", trade.take_profit * 1.05)
    mgr.manage_open_trades()

    closed = store.closed_trades_for("SOL")
    assert closed and closed[-1].status == TradeStatus.CLOSED
