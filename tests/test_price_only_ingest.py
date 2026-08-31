"""Price-only mode must not silently fall back to mock social data."""

from smt.config import MockSource, RedditSource, Settings, SourcesConfig, XSource, get_universe
from smt.ingest.base import build_collectors


def test_disabled_x_and_reddit_does_not_start_mock():
    sources = SourcesConfig(
        reddit=RedditSource(enabled=False),
        x=XSource(enabled=False),
        mock=MockSource(enabled=False),
    )
    collectors = build_collectors(Settings(), sources, get_universe())
    assert collectors == []


def test_mock_still_starts_when_explicitly_enabled():
    sources = SourcesConfig(
        reddit=RedditSource(enabled=False),
        x=XSource(enabled=False),
        mock=MockSource(enabled=True),
    )
    collectors = build_collectors(Settings(), sources, get_universe())
    assert [c.source_name for c in collectors] == ["mock"]
