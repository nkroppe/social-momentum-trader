"""X / Twitter collector (official API v2 recent search, budget-capped).

Uses the pay-per-use X API with a hard monthly read budget persisted to disk.
Each tweet returned from search counts as one read toward the budget.

Requires `X_BEARER_TOKEN` and `x.enabled: true` in config/sources.yaml.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..config import Settings, SignalsConfig, UniverseConfig, XSource, get_signals
from ..logging_setup import get_logger
from ..models import SocialEvent
from .base import extract_tickers
from .quality import QualityFilter

log = get_logger("smt.ingest.x")

API_BASE = "https://api.twitter.com/2"


class ReadBudgetExhausted(Exception):
    """Raised when the monthly X read budget is exhausted."""


class ReadBudget:
    """Persist monthly tweet-read counts to a JSON file."""

    def __init__(self, path: Path, monthly_limit: int):
        self.path = path
        self.monthly_limit = monthly_limit
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _month_key(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m")

    def _load(self) -> dict:
        if not self.path.exists():
            return {"month": self._month_key(), "reads": 0}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"month": self._month_key(), "reads": 0}
        if data.get("month") != self._month_key():
            return {"month": self._month_key(), "reads": 0}
        return data

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    @property
    def reads_used(self) -> int:
        return int(self._load().get("reads", 0))

    @property
    def remaining(self) -> int:
        return max(0, self.monthly_limit - self.reads_used)

    def consume(self, count: int) -> None:
        if count <= 0:
            return
        data = self._load()
        data["reads"] = int(data.get("reads", 0)) + count
        self._save(data)

    def check(self, needed: int = 1) -> None:
        if self.reads_used + needed > self.monthly_limit:
            raise ReadBudgetExhausted(
                f"X read budget exhausted ({self.reads_used}/{self.monthly_limit} this month)"
            )


class XCollector:
    source_name = "x"

    def __init__(
        self,
        settings: Settings,
        cfg: XSource,
        universe: UniverseConfig,
        signals: SignalsConfig | None = None,
    ):
        if not settings.x_bearer_token:
            raise ValueError("X_BEARER_TOKEN is required when the X collector is enabled")
        self.settings = settings
        self.cfg = cfg
        self.universe = universe
        self.quality = QualityFilter(signals if signals is not None else get_signals())
        self.budget = ReadBudget(
            Path("./data/x_budget.json"),
            settings.x_monthly_read_budget,
        )
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {settings.x_bearer_token}"},
            timeout=30.0,
        )

    def _search_queries(self) -> list[str]:
        """Build queries, excluding retweets/replies server-side where possible.

        Filtering at the API means the read budget buys original posts instead
        of amplification noise.
        """
        suffix = ""
        if self.cfg.exclude_retweets:
            suffix += " -is:retweet"
        if self.cfg.exclude_replies:
            suffix += " -is:reply"

        queries = [f"{kw}{suffix}" for kw in self.cfg.keywords]
        for account in self.cfg.watch_accounts:
            handle = account.lstrip("@").strip()
            if handle:
                queries.append(f"from:{handle}{suffix}")
        return queries

    def _parse_tweets(self, payload: dict) -> list[SocialEvent]:
        tweets = payload.get("data") or []
        users = {u["id"]: u for u in (payload.get("includes") or {}).get("users", [])}
        events: list[SocialEvent] = []

        for tweet in tweets:
            text = tweet.get("text", "")
            tickers = extract_tickers(text, self.universe)
            if not tickers:
                continue

            author_id = tweet.get("author_id", "")
            user = users.get(author_id, {})
            username = user.get("username", author_id)
            followers = (user.get("public_metrics") or {}).get("followers_count")
            is_retweet = any(
                ref.get("type") == "retweeted" for ref in (tweet.get("referenced_tweets") or [])
            )

            verdict = self.quality.evaluate(
                text,
                author=f"@{username}" if username else "",
                followers=followers,
                is_retweet=is_retweet,
            )
            if not verdict.keep:
                continue

            created = datetime.fromisoformat(
                tweet["created_at"].replace("Z", "+00:00")
            ).astimezone(UTC)
            tweet_id = tweet["id"]
            for ticker in tickers:
                events.append(
                    SocialEvent(
                        source=self.source_name,
                        external_id=tweet_id,
                        ticker=ticker,
                        author=f"@{username}" if username else "",
                        text=text[:2000],
                        url=f"https://x.com/{username}/status/{tweet_id}",
                        weight=self.cfg.mention_weight,
                        sentiment=verdict.sentiment,
                        author_followers=int(followers or 0),
                        text_hash=verdict.fingerprint,
                        created_at=created,
                    )
                )
        return events

    def _search(self, query: str) -> list[SocialEvent]:
        max_results = min(max(self.cfg.max_results_per_query, 10), 100)
        if self.budget.remaining <= 0:
            raise ReadBudgetExhausted("X read budget exhausted")

        params = {
            "query": query,
            "max_results": max_results,
            "tweet.fields": "created_at,author_id,referenced_tweets",
            "expansions": "author_id",
            "user.fields": "username,public_metrics",
        }
        resp = self._client.get(f"{API_BASE}/tweets/search/recent", params=params)

        if resp.status_code == 429:
            log.warning("X API rate limited on query %r; backing off this poll", query)
            return []

        resp.raise_for_status()
        payload = resp.json()
        tweet_count = len(payload.get("data") or [])
        self.budget.consume(tweet_count)
        log.debug(
            "x search %r -> %d tweets (%d/%d reads used this month)",
            query,
            tweet_count,
            self.budget.reads_used,
            self.settings.x_monthly_read_budget,
        )
        return self._parse_tweets(payload)

    def collect(self) -> list[SocialEvent]:
        if self.budget.remaining <= 0:
            log.warning(
                "X monthly read budget exhausted (%d/%d); skipping poll",
                self.budget.reads_used,
                self.settings.x_monthly_read_budget,
            )
            return []

        queries = self._search_queries()
        if not queries:
            log.warning("X collector enabled but no keywords or watch_accounts configured")
            return []

        events: list[SocialEvent] = []
        for query in queries:
            if self.budget.remaining <= 0:
                break
            try:
                events.extend(self._search(query))
            except ReadBudgetExhausted:
                log.warning("X read budget hit mid-poll; stopping")
                break
            except httpx.HTTPStatusError as exc:
                log.warning("x search HTTP error for %r: %s", query, exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("x search failed for %r: %s", query, exc)

        if self.quality.dropped:
            log.info(
                "x quality filter: kept %d events, dropped %s",
                len(events),
                self.quality.summary(),
            )
        return events

    def close(self) -> None:
        self._client.close()
