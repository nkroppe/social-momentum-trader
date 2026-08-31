"""X collector using uncensored recent counts and event-triggered samples.

Uses the official API v2 with a persisted dollar ledger shared by count requests
and distinct posts returned from recent search.

Requires `X_BEARER_TOKEN` and `x.enabled: true` in config/sources.yaml.
"""

from __future__ import annotations

import json
import math
import time
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import mean, pstdev
from typing import TYPE_CHECKING

import httpx

from ..config import Settings, SignalsConfig, UniverseConfig, XSource, get_signals
from ..logging_setup import get_logger
from ..models import SocialCount, SocialEvent
from .base import extract_tickers
from .quality import QualityFilter

if TYPE_CHECKING:
    from ..store import Store

log = get_logger("smt.ingest.x")

API_BASE = "https://api.twitter.com/2"


class ReadBudgetExhausted(Exception):
    """Raised when the monthly X read budget is exhausted."""


class BudgetStateUnavailable(ReadBudgetExhausted):
    """Raised when paid requests cannot be authorized from local ledger state."""


# Beyond this many distinct posts in a UTC day, stop tracking IDs individually
# and bill every returned post. Overcounting is the safe direction for a spend
# guard, and at any sane budget this ceiling is never reached.
MAX_TRACKED_IDS_PER_DAY = 25_000


class ReadBudget:
    """Dollar ledger for X post reads and recent-count requests.

    The constructor remains compatible with the original read-count API. New
    callers should pass ``monthly_budget_usd`` and both endpoint prices.
    Reservations are persisted before network requests, preventing a multi-query
    poll from crossing either the daily pace or monthly dollar ceiling.
    """

    def __init__(
        self,
        path: Path,
        monthly_limit: int,
        cost_per_read_usd: float = 0.005,
        opening_reads: int = 0,
        *,
        monthly_budget_usd: float | None = None,
        count_request_cost_usd: float = 0.005,
        opening_count_requests: int = 0,
    ):
        self.path = path
        self.monthly_limit = monthly_limit
        self.cost_per_read_usd = cost_per_read_usd
        self.count_request_cost_usd = count_request_cost_usd
        self.monthly_budget_usd = (
            float(monthly_budget_usd)
            if monthly_budget_usd is not None
            else monthly_limit * cost_per_read_usd
        )
        self.opening_reads = max(opening_reads, 0)
        self.opening_count_requests = max(opening_count_requests, 0)
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
            "post_spend_usd": 0.0,
            "day": self._day_key(now),
            "day_reads": 0,
            "day_post_spend_usd": 0.0,
            "count_requests": 0,
            "count_spend_usd": 0.0,
            "day_count_requests": 0,
            "day_count_spend_usd": 0.0,
            "reserved_post_reads": 0,
            "day_reserved_post_reads": 0,
            "day_ids": [],
        }

    def _load(self, now: datetime | None = None) -> dict:
        if not self.path.exists():
            return self._fresh(now)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            message = f"X budget state unreadable at {self.path}; refusing paid requests"
            log.error("%s: %s", message, exc)
            raise BudgetStateUnavailable(message) from exc

        if data.get("month") != self._month_key(now):
            log.info(
                "X read budget: new billing month %s; seeding with %d opening reads "
                "(set X_BUDGET_OPENING_READS from the X console if needed)",
                self._month_key(now),
                self.opening_reads,
            )
            fresh = self._fresh(now)
            if self.opening_reads:
                fresh["reads"] = self.opening_reads
                fresh["post_spend_usd"] = self.opening_reads * self.cost_per_read_usd
            if self.opening_count_requests:
                fresh["count_requests"] = self.opening_count_requests
                fresh["count_spend_usd"] = (
                    self.opening_count_requests * self.count_request_cost_usd
                )
            if self.opening_reads or self.opening_count_requests:
                fresh["started_at"] = (now or datetime.now(UTC)).isoformat()
            return fresh
        if data.get("day") != self._day_key(now):
            # New UTC day: X's dedupe window resets, so previous IDs would be
            # billed again and must not suppress counting.
            data["day"] = self._day_key(now)
            data["day_reads"] = 0
            data["day_post_spend_usd"] = 0.0
            data["day_count_requests"] = 0
            data["day_count_spend_usd"] = 0.0
            data["reserved_post_reads"] = 0
            data["day_reserved_post_reads"] = 0
            data["day_ids"] = []
        data.setdefault("day_ids", [])
        data.setdefault("day_reads", 0)
        data.setdefault(
            "post_spend_usd", int(data.get("reads", 0)) * self.cost_per_read_usd
        )
        data.setdefault(
            "day_post_spend_usd",
            int(data.get("day_reads", 0)) * self.cost_per_read_usd,
        )
        data.setdefault("count_requests", 0)
        data.setdefault(
            "count_spend_usd",
            int(data.get("count_requests", 0)) * self.count_request_cost_usd,
        )
        data.setdefault("day_count_requests", 0)
        data.setdefault(
            "day_count_spend_usd",
            int(data.get("day_count_requests", 0)) * self.count_request_cost_usd,
        )
        data.setdefault("reserved_post_reads", 0)
        data.setdefault("day_reserved_post_reads", 0)
        return data

    def _save(self, data: dict) -> None:
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(data), encoding="utf-8")
        temporary.replace(self.path)

    # ---- Reporting ----------------------------------------------------------

    @property
    def reads_used(self) -> int:
        return int(self._load().get("reads", 0))

    @property
    def remaining(self) -> int:
        if self.cost_per_read_usd <= 0:
            return self.monthly_limit
        return max(0, math.floor(self.remaining_usd / self.cost_per_read_usd + 1e-9))

    @property
    def count_requests_used(self) -> int:
        return int(self._load().get("count_requests", 0))

    @property
    def post_spend_usd(self) -> float:
        return float(self._load().get("post_spend_usd", 0.0))

    @property
    def count_spend_usd(self) -> float:
        return float(self._load().get("count_spend_usd", 0.0))

    @property
    def spend_usd(self) -> float:
        return self.post_spend_usd + self.count_spend_usd

    @property
    def budget_usd(self) -> float:
        return self.monthly_budget_usd

    @property
    def remaining_usd(self) -> float:
        return self._remaining_usd()

    def _remaining_usd(self, now: datetime | None = None) -> float:
        data = self._load(now)
        reserved = int(data.get("reserved_post_reads", 0)) * self.cost_per_read_usd
        spend = float(data.get("post_spend_usd", 0.0)) + float(
            data.get("count_spend_usd", 0.0)
        )
        return max(self.budget_usd - spend - reserved, 0.0)

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
        """Legacy post-read equivalent of today's dollar allowance."""
        if self.cost_per_read_usd <= 0:
            return self.monthly_limit
        return math.floor(self.daily_dollar_allowance(now) / self.cost_per_read_usd + 1e-9)

    def daily_dollar_allowance(self, now: datetime | None = None) -> float:
        """Dollars permitted today, self-correcting over days left."""
        now = now or datetime.now(UTC)
        days_in_month = monthrange(now.year, now.month)[1]
        days_left = max(days_in_month - now.day + 1, 1)
        data = self._load(now)
        day_reserved = (
            int(data.get("day_reserved_post_reads", 0)) * self.cost_per_read_usd
        )
        at_day_start = self._remaining_usd(now) + self.day_spend_usd(now) + day_reserved
        return max(at_day_start / days_left, 0.0)

    def day_used(self, now: datetime | None = None) -> int:
        return int(self._load(now).get("day_reads", 0))

    def day_remaining(self, now: datetime | None = None) -> int:
        if self.cost_per_read_usd <= 0:
            return self.monthly_limit
        return max(math.floor(self.day_remaining_usd(now) / self.cost_per_read_usd + 1e-9), 0)

    def day_count_requests(self, now: datetime | None = None) -> int:
        return int(self._load(now).get("day_count_requests", 0))

    def day_spend_usd(self, now: datetime | None = None) -> float:
        data = self._load(now)
        return float(data.get("day_post_spend_usd", 0.0)) + float(
            data.get("day_count_spend_usd", 0.0)
        )

    def day_remaining_usd(self, now: datetime | None = None) -> float:
        data = self._load(now)
        reserved = int(data.get("day_reserved_post_reads", 0)) * self.cost_per_read_usd
        return max(self.daily_dollar_allowance(now) - self.day_spend_usd(now) - reserved, 0.0)

    # ---- Accounting ---------------------------------------------------------

    def register(self, post_ids: list[str], now: datetime | None = None) -> int:
        """Record returned posts and return how many were newly billable."""
        if not post_ids:
            return 0
        data = self._load(now)
        seen = set(data["day_ids"])

        if len(seen) >= MAX_TRACKED_IDS_PER_DAY:
            fresh_ids = set(post_ids)
        else:
            fresh_ids = {pid for pid in post_ids if pid not in seen}
            data["day_ids"] = list(seen | fresh_ids)
        billed = len(fresh_ids)

        if billed:
            data["reads"] = int(data.get("reads", 0)) + billed
            data["day_reads"] = int(data.get("day_reads", 0)) + billed
            data["post_spend_usd"] = float(data.get("post_spend_usd", 0.0)) + (
                billed * self.cost_per_read_usd
            )
            data["day_post_spend_usd"] = float(
                data.get("day_post_spend_usd", 0.0)
            ) + (billed * self.cost_per_read_usd)
            data.setdefault("started_at", (now or datetime.now(UTC)).isoformat())
        self._save(data)
        return billed

    def check(self, needed: int = 1) -> None:
        needed_usd = needed * self.cost_per_read_usd
        if needed_usd > self.remaining_usd + 1e-12:
            raise ReadBudgetExhausted(
                f"X budget exhausted (${self.spend_usd:.2f}/${self.budget_usd:.2f})"
            )

    def _check_cost(self, cost_usd: float, now: datetime | None = None) -> None:
        if cost_usd > self._remaining_usd(now) + 1e-12:
            raise ReadBudgetExhausted("X monthly dollar budget exhausted")
        if cost_usd > self.day_remaining_usd(now) + 1e-12:
            raise ReadBudgetExhausted("X daily dollar pace exhausted")

    def reserve_count_request(self, now: datetime | None = None) -> None:
        """Charge one recent-count request immediately before sending it."""
        self._check_cost(self.count_request_cost_usd, now)
        data = self._load(now)
        data["count_requests"] = int(data.get("count_requests", 0)) + 1
        data["day_count_requests"] = int(data.get("day_count_requests", 0)) + 1
        data["count_spend_usd"] = float(data.get("count_spend_usd", 0.0)) + (
            self.count_request_cost_usd
        )
        data["day_count_spend_usd"] = float(
            data.get("day_count_spend_usd", 0.0)
        ) + self.count_request_cost_usd
        data.setdefault("started_at", (now or datetime.now(UTC)).isoformat())
        self._save(data)

    def reserve_post_reads(self, maximum: int, now: datetime | None = None) -> None:
        """Persist worst-case search liability before issuing the request."""
        maximum = max(int(maximum), 0)
        self._check_cost(maximum * self.cost_per_read_usd, now)
        data = self._load(now)
        data["reserved_post_reads"] = int(data.get("reserved_post_reads", 0)) + maximum
        data["day_reserved_post_reads"] = (
            int(data.get("day_reserved_post_reads", 0)) + maximum
        )
        self._save(data)

    def settle_post_reads(
        self, post_ids: list[str], reserved: int, now: datetime | None = None
    ) -> int:
        """Release a search reservation and bill distinct returned post IDs."""
        data = self._load(now)
        data["reserved_post_reads"] = max(
            int(data.get("reserved_post_reads", 0)) - reserved, 0
        )
        data["day_reserved_post_reads"] = max(
            int(data.get("day_reserved_post_reads", 0)) - reserved, 0
        )
        self._save(data)
        return self.register(post_ids, now)

    def release_post_reservation(
        self, reserved: int, now: datetime | None = None
    ) -> None:
        self.settle_post_reads([], reserved, now)


@dataclass(frozen=True)
class CountTrigger:
    sample: bool
    reason: str
    zscore: float = 0.0
    relative_multiple: float = 0.0


def count_sample_trigger(current: int, prior: list[int], cfg: XSource) -> CountTrigger:
    """Evaluate a count using prior windows only."""
    if len(prior) < cfg.trigger_min_baseline_windows:
        sample = len(prior) % cfg.cold_start_sample_interval == 0
        return CountTrigger(sample, "cold-start scheduled" if sample else "cold-start hold")
    baseline = [float(value) for value in prior]
    base_mean = mean(baseline)
    std = max(pstdev(baseline), math.sqrt(max(base_mean, 1.0)))
    zscore = (current - base_mean) / std
    relative = current / base_mean if base_mean > 0 else float("inf")
    sample = (
        current >= cfg.trigger_min_count
        and zscore >= cfg.trigger_zscore
        and relative >= cfg.trigger_relative_multiple
    )
    return CountTrigger(
        sample,
        (
            f"count={current} z={zscore:.2f} relative={relative:.2f}x "
            f"baseline={base_mean:.1f}"
        ),
        zscore,
        relative,
    )


class XCollector:
    source_name = "x"

    def __init__(
        self,
        settings: Settings,
        cfg: XSource,
        universe: UniverseConfig,
        signals: SignalsConfig | None = None,
        store: Store | None = None,
    ):
        if not settings.x_bearer_token:
            raise ValueError("X_BEARER_TOKEN is required when the X collector is enabled")
        self.settings = settings
        self.cfg = cfg
        self.universe = universe
        self.store = store
        self.quality = QualityFilter(signals if signals is not None else get_signals())
        self.budget = ReadBudget(
            Path("./data/x_budget.json"),
            settings.x_monthly_read_budget,
            settings.effective_x_post_read_cost_usd,
            settings.x_budget_opening_reads,
            monthly_budget_usd=settings.effective_x_monthly_budget_usd,
            count_request_cost_usd=settings.x_recent_count_request_cost_usd,
        )
        self.count_observations: list[SocialCount] = []
        self._setup_sample_at: dict[str, float] = {}
        self._client = httpx.Client(
            headers={"Authorization": f"Bearer {settings.x_bearer_token}"},
            timeout=30.0,
        )

    def _with_filters(self, query: str) -> str:
        """Build queries, excluding retweets/replies server-side where possible.

        Filtering at the API means the read budget buys original posts instead
        of amplification noise.
        """
        suffix = ""
        if self.cfg.exclude_retweets:
            suffix += " -is:retweet"
        if self.cfg.exclude_replies:
            suffix += " -is:reply"

        return f"{query}{suffix}"

    def _keyword_queries(self) -> list[tuple[str, str]]:
        queries: list[tuple[str, str]] = []
        for keyword in self.cfg.keywords:
            tickers = sorted(extract_tickers(keyword, self.universe))
            if not tickers:
                log.warning("X count keyword %r does not map to a universe ticker", keyword)
                continue
            for ticker in tickers:
                queries.append((ticker, self._with_filters(keyword)))
        return queries

    def _watch_queries(self) -> list[str]:
        queries: list[str] = []
        for account in self.cfg.watch_accounts:
            handle = account.lstrip("@").strip()
            if handle:
                queries.append(self._with_filters(f"from:{handle}"))
        return queries

    def _search_queries(self) -> list[str]:
        """Backward-compatible combined query list."""
        return [query for _, query in self._keyword_queries()] + self._watch_queries()

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
            user_metrics = user.get("public_metrics") or {}
            tweet_metrics = tweet.get("public_metrics") or {}
            is_retweet = any(
                ref.get("type") == "retweeted" for ref in (tweet.get("referenced_tweets") or [])
            )
            is_quote = any(
                ref.get("type") == "quoted" for ref in (tweet.get("referenced_tweets") or [])
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
            author_created = user.get("created_at")
            author_created_at = (
                datetime.fromisoformat(author_created.replace("Z", "+00:00")).astimezone(UTC)
                if author_created
                else None
            )
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
                        author_id=author_id,
                        author_following=int(user_metrics.get("following_count") or 0),
                        author_posts=int(user_metrics.get("tweet_count") or 0),
                        author_created_at=author_created_at,
                        author_verified=bool(user.get("verified", False)),
                        language=str(tweet.get("lang") or ""),
                        possibly_sensitive=bool(tweet.get("possibly_sensitive", False)),
                        is_quote=is_quote,
                        likes=int(tweet_metrics.get("like_count") or 0),
                        reposts=int(tweet_metrics.get("retweet_count") or 0),
                        replies=int(tweet_metrics.get("reply_count") or 0),
                        quotes=int(tweet_metrics.get("quote_count") or 0),
                        bookmarks=int(tweet_metrics.get("bookmark_count") or 0),
                        impressions=int(tweet_metrics.get("impression_count") or 0),
                        text_hash=verdict.fingerprint,
                        created_at=created,
                    )
                )
        return events

    def _search(self, query: str, max_results: int | None = None) -> list[SocialEvent]:
        max_results = min(max(max_results or self.cfg.max_results_per_query, 10), 100)
        self.budget.reserve_post_reads(max_results)

        params = {
            "query": query,
            "max_results": max_results,
            "tweet.fields": (
                "created_at,author_id,referenced_tweets,public_metrics,lang,"
                "entities,possibly_sensitive"
            ),
            "expansions": "author_id",
            "user.fields": "username,public_metrics,created_at,verified",
        }
        try:
            resp = self._client.get(f"{API_BASE}/tweets/search/recent", params=params)
        except Exception:
            self.budget.release_post_reservation(max_results)
            raise

        if resp.status_code == 429:
            self.budget.release_post_reservation(max_results)
            log.warning("X API rate limited on query %r; backing off this poll", query)
            return []

        try:
            resp.raise_for_status()
        except Exception:
            self.budget.release_post_reservation(max_results)
            raise
        payload = resp.json()
        tweets = payload.get("data") or []
        billed = self.budget.settle_post_reads(
            [t["id"] for t in tweets if t.get("id")], max_results
        )
        log.debug(
            "x search %r -> %d tweets (%d newly billable; %d posts, $%.2f cap)",
            query,
            len(tweets),
            billed,
            self.budget.reads_used,
            self.budget.budget_usd,
        )
        return self._parse_tweets(payload)

    def sample_for_ticker(self, ticker: str) -> list[SocialEvent]:
        """Fetch a post sample when a price setup appears and counts did not.

        Count-anomaly sampling is the cheap default. Gen-5 still produced LLM
        reviews with zero ``social_events`` because quiet windows never
        crossed z/relative triggers. One sample per ticker per count window
        keeps the dollar cap intact and gives the shadow ledger posts to label.
        """
        ticker = ticker.upper()
        query = next((q for t, q in self._keyword_queries() if t == ticker), None)
        if query is None:
            return []
        try:
            if self.budget.remaining_usd <= 0 or self.budget.day_remaining_usd() <= 0:
                return []
        except BudgetStateUnavailable:
            return []
        cooldown = max(self.cfg.count_window_minutes, 1) * 60
        now = time.monotonic()
        last = self._setup_sample_at.get(ticker, 0.0)
        if now - last < cooldown:
            return []
        try:
            events = self._search(query, self.cfg.sample_size)
        except ReadBudgetExhausted:
            log.info("X dollar pace hit during setup sample for %s", ticker)
            return []
        except Exception as exc:  # noqa: BLE001
            log.warning("x setup sample failed for %s: %s", ticker, exc)
            return []
        self._setup_sample_at[ticker] = now
        log.info(
            "x setup sample %s: %d events (cooldown %ds)",
            ticker,
            len(events),
            cooldown,
        )
        return events

    def _count_window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        now = now or datetime.now(UTC)
        seconds = self.cfg.count_window_minutes * 60
        end = datetime.fromtimestamp(int(now.timestamp()) // seconds * seconds, tz=UTC)
        return end - timedelta(minutes=self.cfg.count_window_minutes), end

    @staticmethod
    def _parse_recent_count(payload: dict) -> int:
        total = (payload.get("meta") or {}).get("total_tweet_count")
        if total is not None:
            return max(int(total), 0)
        return sum(max(int(item.get("tweet_count", 0)), 0) for item in payload.get("data") or [])

    def _recent_count(
        self,
        ticker: str,
        query: str,
        *,
        now: datetime | None = None,
    ) -> tuple[SocialCount, list[int], bool]:
        start, end = self._count_window(now)
        existing = (
            self.store.social_count_at(self.source_name, ticker, end)
            if self.store is not None
            else None
        )
        if existing is not None:
            log.debug("x count %s window %s already persisted; reusing", ticker, end.isoformat())
            prior_existing = [
                int(item.tweet_count)
                for item in reversed(
                    self.store.recent_social_counts(
                        ticker,
                        limit=max(self.cfg.trigger_min_baseline_windows * 4, 48),
                        before=end,
                    )
                )
            ]
            return existing, prior_existing, False
        prior = (
            [
                int(item.tweet_count)
                for item in reversed(
                    self.store.recent_social_counts(
                        ticker, limit=max(self.cfg.trigger_min_baseline_windows * 4, 48), before=end
                    )
                )
            ]
            if self.store is not None
            else []
        )
        self.budget.reserve_count_request(now)
        params = {
            "query": query,
            "start_time": start.isoformat().replace("+00:00", "Z"),
            "end_time": end.isoformat().replace("+00:00", "Z"),
            "granularity": self.cfg.count_granularity,
        }
        response = self._client.get(f"{API_BASE}/tweets/counts/recent", params=params)
        response.raise_for_status()
        observation = SocialCount(
            source=self.source_name,
            ticker=ticker,
            query=query,
            tweet_count=self._parse_recent_count(response.json()),
            window_start=start,
            window_end=end,
            granularity=self.cfg.count_granularity,
        )
        self.count_observations.append(observation)
        inserted = self.store.add_social_counts([observation]) > 0 if self.store else True
        return observation, prior, inserted

    def collect(self) -> list[SocialEvent]:
        self.quality.reset_dropped()

        try:
            remaining_usd = self.budget.remaining_usd
            day_remaining_usd = self.budget.day_remaining_usd()
        except BudgetStateUnavailable as exc:
            log.error("X collector disabled for this poll: %s", exc)
            return []

        if remaining_usd <= 0:
            log.warning(
                "X monthly budget exhausted (posts=%d counts=%d $%.2f/$%.2f); skipping poll",
                self.budget.reads_used,
                self.budget.count_requests_used,
                self.budget.spend_usd,
                self.budget.budget_usd,
            )
            return []

        # Pace the month so the budget is not consumed in its first few days,
        # which would leave the soak blind for the remaining three weeks.
        if day_remaining_usd <= 0:
            log.info(
                "X daily dollar pace reached ($%.3f/$%.3f today); skipping poll",
                self.budget.day_spend_usd(),
                self.budget.daily_dollar_allowance(),
            )
            return []

        keyword_queries = self._keyword_queries()
        watch_queries = self._watch_queries()
        if not keyword_queries and not watch_queries:
            log.warning("X collector enabled but no keywords or watch_accounts configured")
            return []

        sample_queries: list[str] = []
        if self.cfg.counts_enabled:
            for ticker, query in keyword_queries:
                try:
                    observation, prior, inserted = self._recent_count(ticker, query)
                    trigger = count_sample_trigger(observation.tweet_count, prior, self.cfg)
                    log.info(
                        "x count %s [%s..%s] = %d; %s",
                        ticker,
                        observation.window_start.isoformat(),
                        observation.window_end.isoformat(),
                        observation.tweet_count,
                        trigger.reason,
                    )
                    if inserted and trigger.sample:
                        sample_queries.append(query)
                except ReadBudgetExhausted:
                    log.warning("X dollar pace hit during count poll; stopping counts")
                    break
                except Exception as exc:  # noqa: BLE001
                    # No fabricated zero: broad sampling fails closed for this symbol.
                    log.warning("x recent count failed for %s (%r): %s", ticker, query, exc)
        else:
            sample_queries.extend(query for _, query in keyword_queries)
        sample_queries.extend(watch_queries)

        events: list[SocialEvent] = []
        for query in sample_queries:
            if self.budget.remaining_usd <= 0 or self.budget.day_remaining_usd() <= 0:
                break
            try:
                events.extend(self._search(query, self.cfg.sample_size))
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
