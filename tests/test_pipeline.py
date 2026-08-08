"""Smoke tests for the core pipeline: extraction, scoring, risk gate, exits."""

from __future__ import annotations

import uuid
from datetime import timedelta

from _helpers import make_store, make_strategy, make_universe, social_only_market_cfg

from smt.config import UniverseConfig
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


def _cashtag_universe() -> UniverseConfig:
    return UniverseConfig(
        quote_currency="USD",
        symbols={
            "CAP": {"product_id": "CAP-USD", "aliases": ["$cap"], "require_cashtag": True},
            "PUMP": {
                "product_id": "PUMP-USD",
                "aliases": ["pumpfun", "pump.fun", "$pump"],
                "require_cashtag": True,
            },
            "SOL": {"product_id": "SOL-USD", "aliases": ["sol", "solana"]},
        },
    )


def test_cashtag_only_ticker_ignores_the_bare_word():
    """`CAP` collides with everyday crypto vocabulary, so require the $ form."""
    u = _cashtag_universe()
    assert extract_tickers("what's the market cap on this", u) == set()
    assert extract_tickers("no cap this is a low cap gem", u) == set()
    assert extract_tickers("CAP is going to run", u) == set()


def test_cashtag_only_ticker_still_matches_the_cashtag():
    u = _cashtag_universe()
    assert extract_tickers("accumulating $CAP here", u) == {"CAP"}
    # Mixed post: the cashtag rule must not suppress ordinary aliases.
    assert extract_tickers("$CAP and solana both bid", u) == {"CAP", "SOL"}


def test_cashtag_rule_applies_to_the_symbol_not_descriptive_aliases():
    """"pump" is ambiguous; "pumpfun" is not, so only the symbol needs the $."""
    u = _cashtag_universe()
    assert extract_tickers("classic pump and dump", u) == set()
    assert extract_tickers("pumpfun volume is climbing", u) == {"PUMP"}
    assert extract_tickers("$PUMP breaking out", u) == {"PUMP"}
    # Word boundaries already handled the inflected forms.
    assert extract_tickers("this thing is pumping", u) == set()


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
                    author=f"baseline{i}",
                    text="sol",
                    created_at=ts,
                    weight=1.0,
                )
            ]
        )
    # Burst now across two sources, each post from its own bullish account.
    for src in ("reddit", "x"):
        store.add_events(
            [
                SocialEvent(
                    source=src,
                    external_id=uuid.uuid4().hex,
                    ticker="SOL",
                    author=f"{src}user{n}",
                    text="sol breaking out",
                    sentiment=1.0,
                    created_at=utcnow(),
                    weight=1.0,
                )
                for n in range(12)
            ]
        )

    scorer = MomentumScorer(store, u, st.scorer_bucket_minutes, st.scorer_lookback_buckets)
    result = scorer.score_ticker("SOL")
    assert result.zscore >= st.signal_min_zscore
    assert result.distinct_sources >= 2
    assert result.distinct_authors >= st.signal_min_distinct_authors
    assert result.bullish_ratio == 1.0

    # Price gates off so this exercises the social gate in isolation.
    engine = SignalEngine(st, u, market_cfg=social_only_market_cfg())
    cands = engine.candidates(scorer.score_all())
    assert any(c.ticker == "SOL" for c in cands)


def test_bearish_burst_is_rejected(tmp_path):
    """A crash generates huge chatter; mention count alone would buy it."""
    store = make_store(tmp_path)
    u = make_universe()
    st = make_strategy()

    for i in range(st.scorer_lookback_buckets, 1, -1):
        ts = utcnow() - timedelta(minutes=st.scorer_bucket_minutes * i - 1)
        store.add_events(
            [
                SocialEvent(
                    source="x",
                    external_id=uuid.uuid4().hex,
                    ticker="SOL",
                    author=f"baseline{i}",
                    text="sol",
                    created_at=ts,
                    weight=1.0,
                )
            ]
        )
    store.add_events(
        [
            SocialEvent(
                source="x",
                external_id=uuid.uuid4().hex,
                ticker="SOL",
                author=f"panic{n}",
                text="sol getting liquidated, total capitulation",
                sentiment=-1.0,
                created_at=utcnow(),
                weight=1.0,
            )
            for n in range(24)
        ]
    )

    scorer = MomentumScorer(store, u, st.scorer_bucket_minutes, st.scorer_lookback_buckets)
    result = scorer.score_ticker("SOL")
    assert result.zscore >= st.signal_min_zscore  # attention really did spike
    assert result.bullish_ratio == 0.0

    engine = SignalEngine(st, u, market_cfg=social_only_market_cfg())
    assert engine.candidates(scorer.score_all()) == []


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
