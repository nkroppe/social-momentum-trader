"""Smoke tests for the core pipeline: extraction, scoring, risk gate, exits."""

from __future__ import annotations

import uuid
from datetime import timedelta

from _helpers import make_store, make_strategy, make_universe

from smt.ingest.base import extract_tickers
from smt.models import SocialEvent, TradeStatus, utcnow
from smt.scorer import MomentumScorer
from smt.trader.risk import RiskGate
from smt.trader.signals import SignalEngine, TradeCandidate


def test_extract_tickers():
    u = make_universe()
    found = extract_tickers("I love $SOL and Bitcoin but not xyz", u)
    assert found == {"SOL", "BTC"}
    # substring guard: "solar" should not match "sol"
    assert extract_tickers("solar panels", u) == set()


def test_velocity_signal_fires_on_burst(tmp_path):
    store = make_store(tmp_path)
    u = make_universe()
    st = make_strategy()

    # Baseline in older buckets.
    for i in range(st.scorer_lookback_buckets, 1, -1):
        ts = utcnow() - timedelta(minutes=st.scorer_bucket_minutes * i - 1)
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
    for src in ("reddit", "x"):
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

    scorer = MomentumScorer(store, u, st.scorer_bucket_minutes, st.scorer_lookback_buckets)
    result = scorer.score_ticker("SOL")
    assert result.zscore >= st.signal_min_zscore
    assert result.distinct_sources >= 2

    engine = SignalEngine(st, u)
    cands = engine.candidates(scorer.score_all())
    assert any(c.ticker == "SOL" for c in cands)


def test_risk_gate_blocks_over_limits(tmp_path):
    store = make_store(tmp_path)
    st = make_strategy(max_open_positions=0)
    gate = RiskGate(store)
    cand = TradeCandidate("SOL", "SOL-USD", 5.0, 20, 2, "x", st.name)
    decision = gate.evaluate(cand, st, equity_alloc=5000, start_equity_alloc=5000)
    assert not decision.approved


def test_paper_take_profit_closes(tmp_path):
    from smt.config import Settings
    from smt.trader.manager import TradeManager
    from smt.trader.paper import PaperBroker

    store = make_store(tmp_path)
    u = make_universe()
    st = make_strategy()
    broker = PaperBroker(seed=1)
    settings = Settings(paper_start_equity=5000)
    mgr = TradeManager(settings, u, store, broker)

    cand = TradeCandidate("SOL", "SOL-USD", 5.0, 20, 2, "x", st.name)
    trade = mgr.open_position(cand, 500, st)
    assert trade.status == TradeStatus.OPEN

    broker.set_price("SOL-USD", trade.take_profit * 1.05)
    mgr.manage_open_trades()

    closed = store.closed_trades_for("SOL")
    assert closed and closed[-1].status == TradeStatus.CLOSED
