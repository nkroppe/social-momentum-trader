"""Deterministic coverage for count-driven sampling and shadow decisions."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
from _helpers import make_store, make_strategy, make_universe
from sqlalchemy import inspect, text

from smt.config import Settings, SignalsConfig, XSource, get_signals
from smt.ingest.x import ReadBudget, XCollector, count_sample_trigger
from smt.llm.coordinator import LLMCoordinator
from smt.llm.judge import JudgeDecision
from smt.models import SocialCount, SocialEvent, utcnow
from smt.run import Runner
from smt.scorer import MomentumScorer, ScoreResult
from smt.store import Store
from smt.trader.signals import PriceSetup, SignalEngine, TradeCandidate


def _response(payload: dict, *, error: bool = False) -> MagicMock:
    response = MagicMock()
    response.status_code = 500 if error else 200
    response.json.return_value = payload
    if error:
        request = httpx.Request("GET", "https://api.twitter.com")
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "failed", request=request, response=httpx.Response(500, request=request)
        )
    return response


def test_recent_count_parser_trigger_and_cold_start():
    assert XCollector._parse_recent_count(
        {"data": [{"tweet_count": 2}, {"tweet_count": 3}, {"tweet_count": 5}]}
    ) == 10
    assert XCollector._parse_recent_count(
        {"meta": {"total_tweet_count": 42}, "data": [{"tweet_count": 1}]}
    ) == 42
    cfg = XSource(
        trigger_min_count=8,
        trigger_zscore=2,
        trigger_relative_multiple=2,
        trigger_min_baseline_windows=3,
        cold_start_sample_interval=12,
    )
    assert count_sample_trigger(1, [], cfg).sample
    assert not count_sample_trigger(20, [1], cfg).sample
    assert count_sample_trigger(20, [2, 2, 2], cfg).sample
    assert not count_sample_trigger(7, [2, 2, 2], cfg).sample


def test_count_failure_does_not_store_zero_or_search(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = make_store(tmp_path)
    cfg = XSource(enabled=True, keywords=["$SOL"], counts_enabled=True, sample_size=25)
    settings = Settings(x_bearer_token="token")
    with patch("httpx.Client.get", return_value=_response({}, error=True)) as get:
        assert XCollector(settings, cfg, make_universe(), store=store).collect() == []
    assert get.call_count == 1
    assert not store.has_social_counts("SOL")


def test_count_trigger_persists_observation_then_samples_posts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = make_store(tmp_path)
    cfg = XSource(
        enabled=True,
        keywords=["$SOL"],
        counts_enabled=True,
        trigger_min_baseline_windows=3,
        sample_size=25,
    )
    settings = Settings(x_bearer_token="token")
    collector = XCollector(settings, cfg, make_universe(), store=store)
    start, end = collector._count_window()
    store.add_social_counts(
        [
            SocialCount(
                source="x",
                ticker="SOL",
                query="$SOL",
                tweet_count=2,
                window_start=start - timedelta(minutes=30 * offset),
                window_end=end - timedelta(minutes=30 * offset),
            )
            for offset in (3, 2, 1)
        ]
    )
    count_payload = {
        "meta": {"total_tweet_count": 20},
        "data": [{"tweet_count": 1}],
    }
    search_payload = {
        "data": [
            {
                "id": "123",
                "text": "$SOL has a strong breakout with real momentum",
                "created_at": end.isoformat().replace("+00:00", "Z"),
                "author_id": "42",
                "lang": "en",
                "public_metrics": {"like_count": 9},
            }
        ],
        "includes": {
            "users": [
                {
                    "id": "42",
                    "username": "analyst",
                    "verified": True,
                    "created_at": "2020-01-01T00:00:00Z",
                    "public_metrics": {
                        "followers_count": 5000,
                        "following_count": 100,
                        "tweet_count": 900,
                    },
                }
            ]
        },
    }
    with patch(
        "httpx.Client.get",
        side_effect=[_response(count_payload), _response(search_payload)],
    ) as get:
        events = collector.collect()
    assert get.call_count == 2
    assert "max_results" not in get.call_args_list[0].kwargs["params"]
    assert len(events) == 1
    assert events[0].author_id == "42"
    assert events[0].author_verified is True
    assert events[0].author_following == 100
    assert events[0].likes == 9
    assert events[0].language == "en"
    assert store.recent_social_counts("SOL", 1)[0].tweet_count == 20
    assert collector.budget.count_requests_used == 1
    assert collector.budget.reads_used == 1


def test_duplicate_aligned_count_window_reuses_without_spend_or_sample(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    store = make_store(tmp_path)
    cfg = XSource(enabled=True, keywords=["$SOL"], counts_enabled=True)
    collector = XCollector(
        Settings(x_bearer_token="token"), cfg, make_universe(), store=store
    )
    start, end = collector._count_window()
    store.add_social_counts(
        [
            SocialCount(
                source="x",
                ticker="SOL",
                query="$SOL",
                tweet_count=100,
                window_start=start,
                window_end=end,
            )
        ]
    )
    with patch("httpx.Client.get") as get:
        assert collector.collect() == []
    get.assert_not_called()
    assert collector.budget.count_requests_used == 0
    assert collector.budget.reads_used == 0


def test_corrupt_budget_state_skips_all_x_api_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    budget_path = tmp_path / "data" / "x_budget.json"
    budget_path.parent.mkdir(parents=True)
    budget_path.write_text("{corrupt", encoding="utf-8")
    cfg = XSource(enabled=True, keywords=["$SOL"], counts_enabled=True)
    with patch("httpx.Client.get") as get:
        collector = XCollector(
            Settings(x_bearer_token="token"), cfg, make_universe()
        )
        assert collector.collect() == []
    get.assert_not_called()


def test_rich_x_fields_round_trip(tmp_path):
    store = make_store(tmp_path)
    event = SocialEvent(
        source="x",
        external_id="rich",
        ticker="SOL",
        author="@analyst",
        author_id="42",
        author_followers=5000,
        author_following=100,
        author_posts=900,
        author_created_at=datetime(2020, 1, 1, tzinfo=UTC),
        author_verified=True,
        language="en",
        possibly_sensitive=True,
        is_quote=True,
        likes=10,
        reposts=4,
        replies=3,
        quotes=2,
        bookmarks=1,
        impressions=1000,
        created_at=utcnow() - timedelta(minutes=1),
        ingested_at=utcnow(),
    )
    assert store.add_events([event]) == 1
    loaded = store.recent_social_events("SOL", 1)[0]
    assert loaded.author_id == "42"
    assert loaded.author_verified is True
    assert loaded.likes == 10
    assert loaded.impressions == 1000
    assert loaded.created_at != loaded.ingested_at


def test_dollar_ledger_reserves_both_endpoints_and_migrates(tmp_path):
    path = tmp_path / "budget.json"
    now = datetime.now(UTC)
    path.write_text(
        (
            f'{{"month":"{now:%Y-%m}","reads":2,'
            f'"day":"{now:%Y-%m-%d}","day_reads":2}}'
        ),
        encoding="utf-8",
    )
    budget = ReadBudget(
        path,
        20_000,
        0.005,
        monthly_budget_usd=100,
        count_request_cost_usd=0.01,
    )
    budget.reserve_count_request(now)
    budget.reserve_post_reads(3, now)
    assert budget.remaining_usd == pytest.approx(99.965)
    budget.settle_post_reads(["a"], 3, now)
    assert budget.reads_used == 3
    assert budget.count_requests_used == 1
    assert budget.post_spend_usd == pytest.approx(0.015)
    assert budget.count_spend_usd == pytest.approx(0.01)
    assert not path.with_suffix(".json.tmp").exists()
    repriced = ReadBudget(
        path,
        20_000,
        0.02,
        monthly_budget_usd=100,
        count_request_cost_usd=0.03,
    )
    assert repriced.post_spend_usd == pytest.approx(0.015)
    assert repriced.count_spend_usd == pytest.approx(0.01)


def test_count_based_scorer_never_uses_sample_size_as_volume(tmp_path):
    store = make_store(tmp_path)
    end = Store._aligned_end(utcnow(), 30)
    values = [2, 2, 2, 2, 2, 2, 2, 20]
    store.add_social_counts(
        [
            SocialCount(
                source="x",
                ticker="SOL",
                query="$SOL",
                tweet_count=value,
                window_start=end - timedelta(minutes=30 * (len(values) - index)),
                window_end=end - timedelta(minutes=30 * (len(values) - index - 1)),
            )
            for index, value in enumerate(values)
        ]
    )
    store.add_events(
        [
            SocialEvent(
                source="x",
                external_id=f"sample-{index}",
                ticker="SOL",
                author=f"author-{index}",
                sentiment=1,
                likes=10,
                created_at=utcnow(),
            )
            for index in range(3)
        ]
    )
    result = MomentumScorer(store, make_universe(), 30, 8).score_ticker("SOL")
    assert result.baseline_kind == "count_trailing"
    assert result.mentions_window == sum(values)
    assert result.recent == 20
    assert result.distinct_authors == 3
    assert result.engagement_total == 30


def test_count_windows_aggregate_to_swing_bucket_sizes(tmp_path):
    store = make_store(tmp_path)
    end = Store._aligned_end(utcnow(), 120)
    values = list(range(1, 9))
    store.add_social_counts(
        [
            SocialCount(
                source="x",
                ticker="SOL",
                query="$SOL",
                tweet_count=value,
                window_start=end - timedelta(minutes=30 * (len(values) - index)),
                window_end=end - timedelta(minutes=30 * (len(values) - index - 1)),
            )
            for index, value in enumerate(values)
        ]
    )
    assert store.count_buckets("SOL", 120, 2, now=end) == [10.0, 26.0]


def test_missing_current_count_bucket_is_neutral_without_event_fallback(tmp_path):
    store = make_store(tmp_path)
    end = Store._aligned_end(utcnow(), 30)
    store.add_social_counts(
        [
            SocialCount(
                source="x",
                ticker="SOL",
                query="$SOL",
                tweet_count=2,
                window_start=end - timedelta(minutes=30 * (offset + 1)),
                window_end=end - timedelta(minutes=30 * offset),
            )
            for offset in range(1, 8)
        ]
    )
    store.add_events(
        [
            SocialEvent(
                source="x",
                external_id=f"burst-{index}",
                ticker="SOL",
                author=f"author-{index}",
                created_at=utcnow(),
            )
            for index in range(25)
        ]
    )
    result = MomentumScorer(store, make_universe(), 30, 8).score_ticker("SOL")
    assert result.zscore == 0
    assert result.recent == 0
    assert result.baseline_kind == "count_missing"
    assert result.mentions_window == 14


def test_incomplete_swing_bucket_is_missing_not_zero(tmp_path):
    store = make_store(tmp_path)
    end = Store._aligned_end(utcnow(), 120)
    values = list(range(1, 9))
    store.add_social_counts(
        [
            SocialCount(
                source="x",
                ticker="SOL",
                query="$SOL",
                tweet_count=value,
                window_start=end - timedelta(minutes=30 * (len(values) - index)),
                window_end=end - timedelta(minutes=30 * (len(values) - index - 1)),
            )
            for index, value in enumerate(values)
            if index != 2
        ]
    )
    assert store.count_buckets("SOL", 120, 2, now=end) == [None, 26.0]


def _low_social_score() -> ScoreResult:
    return ScoreResult(
        ticker="SOL",
        zscore=0,
        recent=0,
        baseline_mean=2,
        mentions_window=1,
        distinct_sources=0,
        distinct_authors=0,
        bullish_ratio=0,
        directional_posts=10,
        baseline_kind="count_trailing",
        reason="test",
    )


def test_shadow_social_reject_yields_price_candidate_without_resize(monkeypatch):
    signals = get_signals().model_copy(deep=True)
    signals.social_decision_mode = "shadow"
    engine = SignalEngine(make_strategy(), make_universe(), signals, market=MagicMock())
    setup = PriceSetup(
        name="breakout_retest",
        entry_price=100,
        structure_stop=95,
        stop_pct=0.05,
        atr_pct=0.02,
        conviction=0.85,
        metadata={"trigger_ts": "2026-08-09T12:00:00Z"},
    )
    monkeypatch.setattr(engine, "_price_setup", lambda *_args: setup)
    monkeypatch.setattr(engine, "_regime", lambda: (True, "test"))
    candidate = engine.candidates([_low_social_score()])[0]
    assert candidate.social_decision == "would_reject"
    assert candidate.size_multiplier == pytest.approx(0.85)


@pytest.mark.parametrize(
    "decision",
    [
        JudgeDecision("pending", False, False, 0, 0, "pending", "pending", "k"),
        JudgeDecision("complete", False, True, 0.1, 0.9, "exploit", "credible veto", "k"),
    ],
)
def test_shadow_llm_pending_or_veto_never_blocks_or_resizes(tmp_path, decision):
    coordinator = LLMCoordinator.__new__(LLMCoordinator)
    coordinator.enabled = True
    coordinator.store = make_store(tmp_path)
    coordinator.cfg = SimpleNamespace(
        judge=SimpleNamespace(tiers=["mid"], max_social_posts=12, max_post_chars=500)
    )
    coordinator.judge = SimpleNamespace(evaluate=lambda _context: decision)
    coordinator.signals = SignalsConfig(social_decision_mode="shadow")
    candidate = TradeCandidate(
        "SOL",
        "SOL-USD",
        5,
        20,
        1,
        "test",
        tier="mid",
        size_multiplier=0.8,
        setup="breakout_retest",
    )
    assert coordinator.review_candidate(candidate)
    assert candidate.size_multiplier == 0.8
    assert candidate.llm_status == decision.status
    assert candidate.llm_veto == decision.veto


def test_shadow_audit_upserts_pending_then_complete(tmp_path):
    store = make_store(tmp_path)
    runner = Runner.__new__(Runner)
    runner.store = store
    candidate = TradeCandidate(
        "SOL",
        "SOL-USD",
        5,
        20,
        1,
        "test",
        strategy="intraday",
        tier="mid",
        setup="breakout_retest",
        setup_metadata={"trigger_ts": "2026-08-09T12:00:00Z"},
        social_decision="would_reject",
        social_reason="authors below floor",
        llm_status="pending",
        count_volume=20,
        engagement=14,
    )
    runner._audit_candidate(candidate, risk_status="not_evaluated")
    candidate.llm_status = "complete"
    candidate.llm_score = 0.2
    candidate.llm_veto = True
    runner._audit_candidate(candidate, risk_status="approved", risk_reason="price/risk passed")
    audit = store.shadow_decision(candidate.decision_key)
    assert audit is not None
    assert audit.social_decision == "would_reject"
    assert audit.llm_status == "complete"
    assert audit.llm_veto is True
    assert audit.risk_status == "approved"


def test_coordinator_poll_persists_latest_async_shadow_result(tmp_path):
    store = make_store(tmp_path)
    runner = Runner.__new__(Runner)
    runner.store = store
    candidate = TradeCandidate(
        "SOL",
        "SOL-USD",
        5,
        20,
        1,
        "test",
        strategy="swing",
        tier="mid",
        setup="breakout_retest",
        setup_metadata={"trigger_ts": "2026-08-09T12:00:00Z"},
        llm_status="pending",
    )
    runner._audit_candidate(candidate, risk_status="approved")
    stale = JudgeDecision("complete", True, False, 0.9, 0.9, "old", "old", "old-key")
    newest = JudgeDecision(
        "complete", False, True, 0.1, 0.9, "new", "new adverse context", "new-key"
    )
    coordinator = LLMCoordinator.__new__(LLMCoordinator)
    coordinator.enabled = True
    coordinator.signals = SignalsConfig(social_decision_mode="shadow")
    coordinator.store = store
    coordinator.judge = SimpleNamespace(poll_completed=lambda: [stale, newest])
    coordinator._audit_key_by_judge_key = {
        "old-key": candidate.decision_key,
        "new-key": candidate.decision_key,
    }
    coordinator._latest_judge_key_by_audit = {candidate.decision_key: "new-key"}
    coordinator.poll_judgements()
    audit = store.shadow_decision(candidate.decision_key)
    assert audit is not None
    assert audit.llm_status == "complete"
    assert audit.llm_veto is True
    assert audit.llm_reason == "new adverse context"
    assert audit.risk_status == "approved"


def test_runner_step_polls_judgements_without_a_candidate():
    runner = Runner.__new__(Runner)
    runner.kill = SimpleNamespace(is_active=lambda: False)
    runner._killed_notified = False
    runner._last_ingest = time.monotonic()
    runner.sources = SimpleNamespace(poll_interval_seconds=1800)
    runner.evaluate_and_trade = MagicMock()
    runner.llm = SimpleNamespace(poll_judgements=MagicMock())
    runner.manager = SimpleNamespace(manage_open_trades=MagicMock())
    runner.step()
    runner.llm.poll_judgements.assert_called_once_with()


def test_sqlite_migration_adds_rich_columns_and_new_tables(tmp_path):
    path = tmp_path / "legacy.sqlite"
    store = Store(f"sqlite:///{path}")
    with store.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE social_events ("
                "id INTEGER PRIMARY KEY, source VARCHAR(32), external_id VARCHAR(128), "
                "ticker VARCHAR(16), author VARCHAR(128), text TEXT, url VARCHAR(512), "
                "weight FLOAT, created_at TIMESTAMP, ingested_at TIMESTAMP)"
            )
        )
    store.init_db()
    names = set(inspect(store.engine).get_table_names())
    social_columns = {
        column["name"] for column in inspect(store.engine).get_columns("social_events")
    }
    assert {"social_counts", "shadow_decisions"} <= names
    assert {"author_verified", "bookmarks", "impressions"} <= social_columns
