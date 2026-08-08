"""Collector protocol, ticker extraction, and collector factory."""

from __future__ import annotations

import re
from typing import Protocol

from ..config import Settings, SourcesConfig, UniverseConfig
from ..logging_setup import get_logger
from ..models import SocialEvent

log = get_logger("smt.ingest")


class Collector(Protocol):
    source_name: str

    def collect(self) -> list[SocialEvent]:
        """Return newly observed, ticker-tagged social events."""
        ...


def _build_alias_index(universe: UniverseConfig) -> dict[str, str]:
    """Map a lowercased alias -> canonical ticker."""
    index: dict[str, str] = {}
    for ticker, spec in universe.symbols.items():
        index[ticker.lower()] = ticker
        for alias in spec.aliases:
            index[alias.lower().lstrip("$")] = ticker
    return index


# Cache the compiled matcher per universe id to avoid rebuilding each call.
_MATCHER_CACHE: dict[int, tuple[re.Pattern[str], dict[str, str]]] = {}


def _get_matcher(universe: UniverseConfig) -> tuple[re.Pattern[str], dict[str, str]]:
    key = id(universe)
    cached = _MATCHER_CACHE.get(key)
    if cached is not None:
        return cached
    alias_index = _build_alias_index(universe)
    # Longest aliases first so "bitcoin" wins over "btc" substrings.
    aliases = sorted(alias_index.keys(), key=len, reverse=True)
    pattern = re.compile(
        r"(?<![a-z0-9])(?:" + "|".join(re.escape(a) for a in aliases) + r")(?![a-z0-9])",
        re.IGNORECASE,
    )
    _MATCHER_CACHE[key] = (pattern, alias_index)
    return pattern, alias_index


def extract_tickers(text: str, universe: UniverseConfig) -> set[str]:
    """Return the set of tradeable tickers mentioned in text."""
    if not text:
        return set()
    pattern, alias_index = _get_matcher(universe)
    found: set[str] = set()
    for m in pattern.finditer(text):
        alias = m.group(0).lower().lstrip("$")
        ticker = alias_index.get(alias)
        if ticker and universe.tradeable(ticker):
            found.add(ticker)
    return found


def build_collectors(
    settings: Settings, sources: SourcesConfig, universe: UniverseConfig
) -> list[Collector]:
    """Instantiate enabled collectors; fall back to mock so the loop always runs."""
    collectors: list[Collector] = []

    if sources.reddit.enabled and settings.reddit_client_id:
        try:
            from .reddit import RedditCollector

            collectors.append(RedditCollector(settings, sources.reddit, universe))
            log.info("Reddit collector enabled")
        except Exception as exc:  # noqa: BLE001
            log.warning("Reddit collector unavailable: %s", exc)

    if sources.x.enabled and settings.x_bearer_token:
        try:
            from .x_stub import XCollector

            collectors.append(XCollector(settings, sources.x, universe))
            log.info("X collector enabled")
        except Exception as exc:  # noqa: BLE001
            log.warning("X collector unavailable: %s", exc)

    if not collectors or sources.mock.enabled:
        from .mock import MockCollector

        collectors.append(MockCollector(sources.mock, universe))
        if not any(c.source_name != "mock" for c in collectors):
            log.info("No live sources configured -> running with MOCK data only")
    return collectors
