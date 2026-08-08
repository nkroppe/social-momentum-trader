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


def _build_alias_index(universe: UniverseConfig) -> tuple[dict[str, str], set[str]]:
    """Map lowercased alias -> ticker, plus the aliases that require a `$`."""
    index: dict[str, str] = {}
    cashtag_only: set[str] = set()
    for ticker, spec in universe.symbols.items():
        aliases = {ticker.lower()} | {a.lower().lstrip("$") for a in spec.aliases}
        aliases.discard("")
        for alias in aliases:
            index[alias] = ticker
            if spec.require_cashtag:
                cashtag_only.add(alias)
    return index, cashtag_only


# Cache the compiled matcher per universe *content*. Keying on id() is unsound:
# a freed config's address can be reused and hand back another universe's regex.
_MatcherKey = tuple[tuple[str, bool, tuple[str, ...]], ...]
_MATCHER_CACHE: dict[_MatcherKey, tuple[re.Pattern[str], dict[str, str]]] = {}

# Matches nothing, for an empty universe. An empty alternation would instead
# match the empty string at every position.
_NEVER = r"(?!)"


def _alternation(aliases: set[str]) -> str:
    # Longest aliases first so "bitcoin" wins over "btc" substrings.
    return "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))


def _cache_key(universe: UniverseConfig) -> _MatcherKey:
    return tuple(
        (ticker, spec.require_cashtag, tuple(sorted(spec.aliases)))
        for ticker, spec in sorted(universe.symbols.items())
    )


def _get_matcher(universe: UniverseConfig) -> tuple[re.Pattern[str], dict[str, str]]:
    key = _cache_key(universe)
    cached = _MATCHER_CACHE.get(key)
    if cached is not None:
        return cached

    alias_index, cashtag_only = _build_alias_index(universe)
    plain = set(alias_index) - cashtag_only

    branches: list[str] = []
    if cashtag_only:
        branches.append(r"\$(?:" + _alternation(cashtag_only) + r")(?![a-z0-9])")
    if plain:
        # A plain alias still matches when written as a cashtag: the lookbehind
        # excludes only alphanumerics, so "$btc" hits the "btc" alias.
        branches.append(r"(?<![a-z0-9])(?:" + _alternation(plain) + r")(?![a-z0-9])")

    pattern = re.compile("|".join(branches) or _NEVER, re.IGNORECASE)
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

    if sources.x.enabled:
        try:
            from .x import XCollector

            x_collector = XCollector(settings, sources.x, universe)
            collectors.append(x_collector)
            log.info(
                "X collector enabled (budget %d reads/mo, %d remaining)",
                settings.x_monthly_read_budget,
                x_collector.budget.remaining,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("X collector unavailable: %s", exc)

    if not collectors or sources.mock.enabled:
        from .mock import MockCollector

        collectors.append(MockCollector(sources.mock, universe))
        if not any(c.source_name != "mock" for c in collectors):
            log.info("No live sources configured -> running with MOCK data only")
    return collectors
