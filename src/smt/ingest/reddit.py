"""Reddit collector (official API via PRAW). Requires the `live` extra."""

from __future__ import annotations

from datetime import UTC, datetime

from ..config import RedditSource, Settings, UniverseConfig
from ..logging_setup import get_logger
from ..models import SocialEvent
from .base import extract_tickers

log = get_logger("smt.ingest.reddit")


class RedditCollector:
    source_name = "reddit"

    def __init__(self, settings: Settings, cfg: RedditSource, universe: UniverseConfig):
        import praw  # imported lazily; only needed for live ingest

        self.cfg = cfg
        self.universe = universe
        self.client = praw.Reddit(
            client_id=settings.reddit_client_id,
            client_secret=settings.reddit_client_secret,
            user_agent=settings.reddit_user_agent,
            check_for_async=False,
        )
        self.client.read_only = True

    def collect(self) -> list[SocialEvent]:
        events: list[SocialEvent] = []
        for sub in self.cfg.subreddits:
            try:
                for post in self.client.subreddit(sub).new(limit=self.cfg.limit_per_subreddit):
                    text = f"{post.title}\n{getattr(post, 'selftext', '')}"
                    created = datetime.fromtimestamp(post.created_utc, tz=UTC)
                    for ticker in extract_tickers(text, self.universe):
                        events.append(
                            SocialEvent(
                                source=self.source_name,
                                external_id=str(post.id),
                                ticker=ticker,
                                author=str(post.author) if post.author else "",
                                text=text[:2000],
                                url=f"https://reddit.com{post.permalink}",
                                weight=1.0,
                                created_at=created,
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                log.warning("reddit poll failed for r/%s: %s", sub, exc)
        return events
