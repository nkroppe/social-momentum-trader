"""Post-level spam filtering and directional sentiment scoring.

Cashtag streams are heavily farmed by airdrop bots, signal groups, and copy-
paste accounts, so a raw mention count badly overstates genuine attention. This
module runs before events reach the store, so the scorer only ever sees
deduped, non-spam, direction-tagged posts.

Sentiment is lexicon-based on purpose: it is cheap, deterministic, auditable,
and good enough to separate "breaking out" from "getting liquidated", which is
the distinction raw mention counting misses entirely.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass

from ..config import SignalsConfig
from ..logging_setup import get_logger

log = get_logger("smt.ingest.quality")

_URL_RE = re.compile(r"https?://\S+")
_MENTION_RE = re.compile(r"@\w+")
_CASHTAG_RE = re.compile(r"\$[A-Za-z]{2,10}\b")
_NON_WORD_RE = re.compile(r"[^a-z0-9\s]+")
_WS_RE = re.compile(r"\s+")
_RT_PREFIX_RE = re.compile(r"^rt\s+@?\w+:", re.IGNORECASE)

# How far back a negation flips the polarity of a sentiment term.
_NEGATION_SCOPE = 3


def normalize_text(text: str) -> str:
    """Lowercased, URL/mention-stripped text used for dedup and sentiment."""
    lowered = text.lower()
    lowered = _URL_RE.sub(" ", lowered)
    lowered = _MENTION_RE.sub(" ", lowered)
    lowered = _NON_WORD_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", lowered).strip()


def text_fingerprint(text: str) -> str:
    """Stable hash of normalized text, so reworded-only-by-emoji copies collide."""
    return hashlib.sha1(normalize_text(text).encode("utf-8")).hexdigest()[:32]


def count_cashtags(text: str) -> int:
    return len(_CASHTAG_RE.findall(text))


def count_urls(text: str) -> int:
    return len(_URL_RE.findall(text))


def looks_like_retweet(text: str) -> bool:
    return bool(_RT_PREFIX_RE.match(text.strip()))


def sentiment_score(text: str, cfg: SignalsConfig) -> float:
    """Directional score in [-1, 1]; 0.0 means neutral or no signal terms.

    Multi-word terms are matched against the normalized string; single words are
    matched token-wise so a preceding negation can flip them.
    """
    sent = cfg.sentiment
    if not sent.enabled:
        return 0.0

    norm = normalize_text(text)
    if not norm:
        return 0.0
    tokens = norm.split()

    bull = 0
    bear = 0

    def _negated(idx: int) -> bool:
        start = max(0, idx - _NEGATION_SCOPE)
        return any(t in sent.negations for t in tokens[start:idx])

    for terms, is_bull in ((sent.bullish_terms, True), (sent.bearish_terms, False)):
        for term in terms:
            term_norm = normalize_text(term)
            if not term_norm:
                continue
            if " " in term_norm:
                hits = norm.count(term_norm)
                if hits:
                    if is_bull:
                        bull += hits
                    else:
                        bear += hits
                continue
            for i, tok in enumerate(tokens):
                if tok != term_norm:
                    continue
                flipped = _negated(i)
                if is_bull != flipped:
                    bull += 1
                else:
                    bear += 1

    total = bull + bear
    if total == 0:
        return 0.0
    return (bull - bear) / total


@dataclass
class Verdict:
    keep: bool
    reason: str
    sentiment: float = 0.0
    fingerprint: str = ""


class QualityFilter:
    """Stateful spam filter; dedup and author-flood state span polls."""

    def __init__(self, cfg: SignalsConfig):
        self.cfg = cfg
        self._seen: dict[str, float] = {}
        self._author_posts: dict[str, list[float]] = {}
        self.dropped: dict[str, int] = {}

    # ---- Window bookkeeping -------------------------------------------------

    def _window_seconds(self) -> float:
        return self.cfg.spam.dedup_window_minutes * 60.0

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds()
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        pruned: dict[str, list[float]] = {}
        for author, stamps in self._author_posts.items():
            keep = [t for t in stamps if t >= cutoff]
            if keep:
                pruned[author] = keep
        self._author_posts = pruned

    def reset_dropped(self) -> None:
        """Clear per-poll drop tallies so logs compare kept vs dropped fairly."""
        self.dropped.clear()

    def _drop(self, reason: str) -> Verdict:
        self.dropped[reason] = self.dropped.get(reason, 0) + 1
        return Verdict(keep=False, reason=reason)

    # ---- Main entry point ---------------------------------------------------

    def evaluate(
        self,
        text: str,
        author: str = "",
        followers: int | None = None,
        is_retweet: bool = False,
    ) -> Verdict:
        spam = self.cfg.spam
        sentiment = sentiment_score(text, self.cfg)

        if not spam.enabled:
            return Verdict(True, "filters disabled", sentiment, text_fingerprint(text))

        now = time.monotonic()
        self._prune(now)

        if spam.drop_retweets and (is_retweet or looks_like_retweet(text)):
            return self._drop("retweet")

        norm = normalize_text(text)
        if len(norm) < spam.min_text_chars:
            return self._drop("too_short")

        if count_cashtags(text) > spam.max_cashtags_per_post:
            return self._drop("cashtag_spam")

        if count_urls(text) > spam.max_urls_per_post:
            return self._drop("url_spam")

        for phrase in spam.blocklist_phrases:
            if phrase.lower() in norm:
                return self._drop("blocklist")

        # followers is None when the source does not expose it (e.g. Reddit).
        if followers is not None and followers < spam.min_author_followers:
            return self._drop("low_followers")

        fingerprint = text_fingerprint(text)
        if fingerprint in self._seen:
            return self._drop("duplicate_text")

        if author:
            stamps = self._author_posts.setdefault(author, [])
            if len(stamps) >= spam.max_posts_per_author_per_window:
                return self._drop("author_flood")
            stamps.append(now)

        self._seen[fingerprint] = now

        if self.cfg.sentiment.require_directional and sentiment == 0.0:
            return self._drop("neutral_sentiment")

        return Verdict(True, "ok", sentiment, fingerprint)

    def summary(self) -> str:
        if not self.dropped:
            return "no posts dropped"
        parts = sorted(self.dropped.items(), key=lambda kv: -kv[1])
        return " ".join(f"{k}={v}" for k, v in parts)
