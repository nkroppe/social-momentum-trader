"""Tests for spam filtering, sentiment polarity, and tiered signal modes."""

from __future__ import annotations

from _helpers import make_strategy, make_universe, social_only_market_cfg

from smt.config import SignalsConfig, UniverseConfig, get_signals
from smt.ingest.quality import QualityFilter, normalize_text, sentiment_score, text_fingerprint
from smt.scorer import ScoreResult
from smt.trader.signals import SignalEngine


def _cfg() -> SignalsConfig:
    return get_signals()


def _score(ticker: str = "SOL", **overrides) -> ScoreResult:
    base = {
        "ticker": ticker,
        "zscore": 6.0,
        "recent": 30.0,
        "baseline_mean": 2.0,
        "mentions_window": 40,
        "distinct_sources": 1,
        "distinct_authors": 12,
        "bullish_ratio": 0.9,
        "directional_posts": 20,
        "baseline_kind": "trailing",
        "reason": "test",
    }
    base.update(overrides)
    return ScoreResult(**base)


# ---- Sentiment --------------------------------------------------------------


def test_sentiment_separates_direction():
    cfg = _cfg()
    assert sentiment_score("$SOL breaking out to new ath", cfg) > 0
    assert sentiment_score("$SOL getting rekt, total capitulation", cfg) < 0
    assert sentiment_score("$SOL price is 150 dollars today", cfg) == 0.0


def test_sentiment_handles_negation():
    cfg = _cfg()
    assert sentiment_score("this is not bullish at all", cfg) < 0


def test_normalize_and_fingerprint_collapse_near_duplicates():
    a = "$SOL to the MOON!!! https://spam.example/x"
    b = "$sol to the moon @someone"
    assert normalize_text(a) == normalize_text(b)
    assert text_fingerprint(a) == text_fingerprint(b)


# ---- Spam -------------------------------------------------------------------


def test_filter_drops_retweets_and_duplicates():
    f = QualityFilter(_cfg())
    text = "$SOL is breaking out of the range today with strong volume"

    assert f.evaluate(text, author="@a", followers=5000).keep
    # Same text from a different account is still a copy-paste.
    assert not f.evaluate(text, author="@b", followers=5000).keep
    assert not f.evaluate("RT @someone: " + text, author="@c", followers=5000).keep


def test_filter_drops_low_quality_posts():
    f = QualityFilter(_cfg())
    assert not f.evaluate("$SOL", author="@a", followers=9000).keep  # too short
    assert not f.evaluate(
        "free crypto giveaway, dm me now for your $SOL allocation today",
        author="@b",
        followers=9000,
    ).keep
    assert not f.evaluate(
        "$BTC $ETH $SOL $XRP $DOGE $ADA all pumping hard right now today",
        author="@c",
        followers=9000,
    ).keep
    assert not f.evaluate(
        "$SOL looking strong here, real breakout forming", author="@d", followers=3
    ).keep


def test_filter_limits_one_author_flooding():
    f = QualityFilter(_cfg())
    limit = _cfg().spam.max_posts_per_author_per_window
    for i in range(limit):
        assert f.evaluate(f"$SOL breaking out again number {i} today", author="@loud").keep
    assert not f.evaluate("$SOL breaking out one more time today", author="@loud").keep


# ---- Tiered signal modes ----------------------------------------------------


def _universe(tier: str) -> UniverseConfig:
    return UniverseConfig(
        quote_currency="USD",
        symbols={"SOL": {"product_id": "SOL-USD", "aliases": ["sol"], "tier": tier}},
    )


def _engine(tier: str, **strategy_overrides) -> SignalEngine:
    return SignalEngine(
        make_strategy(**strategy_overrides),
        _universe(tier),
        market_cfg=social_only_market_cfg(),
    )


def test_major_tier_never_trades_on_social_alone():
    """Majors trade on trend. With no price evidence, nothing may fire."""
    assert _engine("major").candidates([_score(zscore=40.0)]) == []


def test_trend_mode_requires_price_evidence_even_when_gates_are_off():
    """Disabling confirmation must not turn trend mode into a free pass."""
    engine = SignalEngine(
        make_strategy(), _universe("major"), market_cfg=social_only_market_cfg()
    )
    assert engine.candidates([_score(zscore=40.0, mentions_window=500)]) == []


def test_hybrid_tier_requires_enough_authors():
    engine = _engine("mid")
    assert engine.candidates([_score(distinct_authors=1)]) == []
    assert engine.candidates([_score()]) != []


def test_hybrid_tier_requires_bullish_majority():
    assert _engine("mid").candidates([_score(bullish_ratio=0.1)]) == []


def test_sparse_directional_posts_skip_the_sentiment_gate():
    """Below the minimum sample the ratio is too noisy to block on."""
    cands = _engine("mid").candidates([_score(bullish_ratio=0.0, directional_posts=1)])
    assert len(cands) == 1


def test_tier_is_carried_onto_the_candidate():
    engine = SignalEngine(
        make_strategy(), make_universe(), market_cfg=social_only_market_cfg()
    )
    cands = engine.candidates([_score()])
    assert cands[0].tier == "mid"
