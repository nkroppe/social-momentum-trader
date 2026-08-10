"""Tests for the X / Twitter collector and read budget."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from smt.config import Settings, UniverseConfig, XSource
from smt.ingest.x import ReadBudget, ReadBudgetExhausted, XCollector


def _universe() -> UniverseConfig:
    return UniverseConfig(
        symbols={
            "SOL": {"product_id": "SOL-USD", "aliases": ["sol", "solana", "$sol"]},
        }
    )


def test_read_budget_tracks_and_resets_month(tmp_path):
    path = tmp_path / "x_budget.json"
    budget = ReadBudget(path, monthly_limit=100)
    assert budget.remaining == 100
    budget.register([str(i) for i in range(40)])
    assert budget.reads_used == 40
    assert budget.remaining == 60

    # Simulate month rollover.
    path.write_text('{"month": "1999-01", "reads": 999}', encoding="utf-8")
    assert budget.reads_used == 0
    assert budget.remaining == 100


def test_read_budget_exhausted(tmp_path):
    budget = ReadBudget(tmp_path / "b.json", 5)
    budget.register([str(i) for i in range(5)])
    with pytest.raises(ReadBudgetExhausted):
        budget.check(1)


def test_x_collector_parses_search_response(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(x_bearer_token="test-token", x_monthly_read_budget=1000)
    cfg = XSource(
        enabled=True,
        keywords=["$SOL"],
        max_results_per_query=10,
        mention_weight=2.0,
    )

    payload = {
        "data": [
            {
                "id": "123",
                "text": "Huge momentum on $SOL today",
                "created_at": "2026-08-08T12:00:00Z",
                "author_id": "42",
            }
        ],
        "includes": {"users": [{"id": "42", "username": "cryptotrader"}]},
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.Client.get", return_value=mock_response):
        collector = XCollector(settings, cfg, _universe())
        events = collector.collect()

    assert len(events) == 1
    assert events[0].source == "x"
    assert events[0].ticker == "SOL"
    assert events[0].external_id == "123"
    assert events[0].weight == 2.0
    assert "cryptotrader" in events[0].url
    assert collector.budget.reads_used == 1


def test_x_collector_skips_when_budget_exhausted(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(x_bearer_token="test-token", x_monthly_read_budget=1)
    cfg = XSource(enabled=True, keywords=["$SOL", "$BTC"])

    budget_path = tmp_path / "data" / "x_budget.json"
    budget_path.parent.mkdir(parents=True)
    month = datetime.now(UTC).strftime("%Y-%m")
    budget_path.write_text(f'{{"month": "{month}", "reads": 1}}', encoding="utf-8")

    with patch("httpx.Client.get") as mock_get:
        collector = XCollector(settings, cfg, _universe())
        events = collector.collect()
        mock_get.assert_not_called()

    assert events == []


def test_x_collector_builds_from_watch_accounts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # The strict daily dollar reservation must cover the 25-post sample.
    settings = Settings(x_bearer_token="test-token", x_monthly_read_budget=1000)
    cfg = XSource(enabled=True, watch_accounts=["elonmusk"], keywords=[])

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"data": [], "includes": {"users": []}}
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.Client.get", return_value=mock_response) as mock_get:
        XCollector(settings, cfg, _universe()).collect()
        assert mock_get.call_count == 1
        # Retweets and replies are excluded server-side so the read budget
        # buys original posts.
        assert (
            mock_get.call_args.kwargs["params"]["query"]
            == "from:elonmusk -is:retweet -is:reply"
        )


def test_x_collector_drops_spam_and_scores_sentiment(tmp_path, monkeypatch):
    """Farmed posts never reach the store; kept posts carry a polarity."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(x_bearer_token="test-token", x_monthly_read_budget=1000)
    cfg = XSource(enabled=True, keywords=["$SOL"], mention_weight=2.0)

    payload = {
        "data": [
            {
                "id": "1",
                "text": "$SOL is breaking out of this range, bullish continuation",
                "created_at": "2026-08-08T12:00:00Z",
                "author_id": "10",
            },
            {
                "id": "2",
                "text": "free crypto giveaway! dm me for your $SOL allocation right now",
                "created_at": "2026-08-08T12:01:00Z",
                "author_id": "11",
            },
            {
                "id": "3",
                "text": "$SOL looking great here, strong breakout forming today",
                "created_at": "2026-08-08T12:02:00Z",
                "author_id": "12",
            },
        ],
        "includes": {
            "users": [
                {"id": "10", "username": "real", "public_metrics": {"followers_count": 9000}},
                {"id": "11", "username": "spammer", "public_metrics": {"followers_count": 9000}},
                {"id": "12", "username": "tiny", "public_metrics": {"followers_count": 4}},
            ]
        },
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = payload
    mock_response.raise_for_status = MagicMock()

    with patch("httpx.Client.get", return_value=mock_response):
        events = XCollector(settings, cfg, _universe()).collect()

    # Only the genuine post from an account above the follower floor survives.
    assert [e.external_id for e in events] == ["1"]
    assert events[0].sentiment > 0
    assert events[0].author_followers == 9000
    assert events[0].text_hash
