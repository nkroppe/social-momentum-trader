"""X / Twitter collector (official API v2 recent search, budget-capped).

Uses the pay-per-use X API with a hard monthly read budget persisted to disk.
Each tweet returned from search counts as one read toward the budget.

Requires `X_BEARER_TOKEN` and `x.enabled: true` in config/sources.yaml.
"""

from __future__ import annotations

import json
from calendar import monthrange
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


# Beyond this many distinct posts in a UTC day, stop tracking IDs individually
# and bill every returned post. Overcounting is the safe direction for a spend
# guard, and at any sane budget this ceiling is never reached.
MAX_TRACKED_IDS_PER_DAY = 25_000


class ReadBudget:
    """Track billable X post reads against a monthly cap, paced daily.

    X bills per post returned but deduplicates within a UTC day, so re-reading
    the same post on overlapping polls is free. Counting every returned post
    therefore overstates the bill badly -- measured at ~3.7x on a 5-minute poll
    interval. This tracks distinct post IDs per UTC day instead, which mirrors
    how the charge is actually assessed.

    Spend is also paced: each day may use the remaining monthly allowance
    divided by the days left. Without pacing a month's budget is consumed in
    the first few days and the soak then collects nothing for three weeks.
    """

    def __init__(self, path: Path, monthly_limit: int, cost_per_read_usd: float = 0.005):
        self.path = path
        self.monthly_limit = monthly_limit
        self.cost_per_read_usd = cost_per_read_usd
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---- State --------------------------------------------------------------

    @staticmethod
    def _month_key(now: datetime | None = None) -> str:
        return (now or datetime.now(UTC)).strftime("%Y-%m")

    @staticmethod
    def _day_key(now: datetime | None = None) -> str:
        return (now or datetime.now(UTC)).strftime("%Y-%m-%d")

    def _fresh(self, now: datetime | None = None) -> dict:
        return {
            "month": self._month_key(now),
            "reads": 0,
            "day": self._day_key(now),
            "day_reads": 0,
            "day_ids": [],
        }

    def _load(self, now: datetime | None = None) -> dict:
        if not self.path.exists():
            return self._fresh(now)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return self._fresh(now)

        if data.get("month") != self._month_key(now):
            return self._fresh(now)
        if data.get("day") != self._day_key(now):
            # New UTC day: X's dedupe window resets, so previous IDs would be
            # billed again and must not suppress counting.
            data["day"] = self._day_key(now)
            data["day_reads"] = 0
            data["day_ids"] = []
        data.setdefault("day_ids", [])
        data.setdefault("day_reads", 0)
        return data

    def _save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data), encoding="utf-8")

    # ---- Reporting ----------------------------------------------------------

    @property
    def reads_used(self) -> int:
        return int(self._load().get("reads", 0))

    @property
    def remaining(self) -> int:
        return max(0, self.monthly_limit - self.reads_used)

    @property
    def spend_usd(self) -> float:
        return self.reads_used * self.cost_per_read_usd

    @property
    def budget_usd(self) -> float:
        return self.monthly_limit * self.cost_per_read_usd

    @property
    def started_at(self) -> datetime | None:
        """When this month's first read was recorded.

        A burn rate has to be measured against time actually spent polling. A
        bot started mid-month has spent nothing on the days before it existed,
        so dividing by the elapsed month understates the real rate badly.
        """
        raw = self._load().get("started_at")
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    # ---- Daily pacing -------------------------------------------------------

    def daily_allowance(self, now: datetime | None = None) -> int:
        """Reads permitted today: what was left this morning, over the days left.

        Today's own spending is added back so the allowance holds steady through
        the day instead of shrinking as it is consumed. Self-correcting across
        days -- an underspent day raises tomorrow's allowance.
        """
        now = now or datetime.now(UTC)
        days_in_month = monthrange(now.year, now.month)[1]
        days_left = max(days_in_month - now.day + 1, 1)
        at_day_start = self.remaining + self.day_used(now)
        return max(at_day_start // days_left, 0)

    def day_used(self, now: datetime | None = None) -> int:
        return int(self._load(now).get("day_reads", 0))

    def day_remaining(self, now: datetime | None = None) -> int:
        return max(self.daily_allowance(now) - self.day_used(now), 0)

    # ---- Accounting ---------------------------------------------------------

    def register(self, post_ids: list[str], now: datetime | None = None) -> int:
        """Record returned posts and return how many were newly billable."""
        if not post_ids:
            return 0
        data = self._load(now)
        seen = set(data["day_ids"])

        if len(seen) >= MAX_TRACKED_IDS_PER_DAY:
            billed = len(post_ids)
        else:
            fresh = [pid for pid in post_ids if pid not in seen]
            billed = len(fresh)
            data["day_ids"] = list(seen | set(fresh))

        if billed:
            data["reads"] = int(data.get("reads", 0)) + billed
            data["day_reads"] = int(data.get("day_reads", 0)) + billed
            data.setdefault("started_at", (now or datetime.now(UTC)).isoformat())
        self._save(data)
        return billed

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
            settings.x_read_cost_usd,
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
        tweets = payload.get("data") or []
        billed = self.budget.register([t["id"] for t in tweets if t.get("id")])
        log.debug(
            "x search %r -> %d tweets (%d newly billable; %d/%d this month)",
            query,
            len(tweets),
            billed,
            self.budget.reads_used,
            self.settings.x_monthly_read_budget,
        )
        return self._parse_tweets(payload)

    def collect(self) -> list[SocialEvent]:
        if self.budget.remaining <= 0:
            log.warning(
                "X monthly read budget exhausted (%d/%d, ~$%.2f); skipping poll",
                self.budget.reads_used,
                self.settings.x_monthly_read_budget,
                self.budget.spend_usd,
            )
            return []

        # Pace the month so the budget is not consumed in its first few days,
        # which would leave the soak blind for the remaining three weeks.
        if self.budget.day_remaining() <= 0:
            log.info(
                "X daily read pace reached (%d/%d today); skipping poll",
                self.budget.day_used(),
                self.budget.daily_allowance(),
            )
            return []

        queries = self._search_queries()
        if not queries:
            log.warning("X collector enabled but no keywords or watch_accounts configured")
            return []

        events: list[SocialEvent] = []
        for query in queries:
            if self.budget.remaining <= 0 or self.budget.day_remaining() <= 0:
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
