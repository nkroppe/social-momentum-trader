"""Mock collector: synthesizes plausible social mentions with occasional bursts.

Lets the full pipeline (score -> signal -> risk -> paper fill -> exit) run with
zero external credentials, and deliberately creates momentum spikes so signals
actually fire during a demo/soak.
"""

from __future__ import annotations

import random
import uuid
from datetime import timedelta

from ..config import MockSource, SignalsConfig, UniverseConfig, get_signals
from ..logging_setup import get_logger
from ..models import SocialEvent, utcnow
from .quality import sentiment_score, text_fingerprint

log = get_logger("smt.ingest.mock")

_TEMPLATES = [
    "everyone is talking about {a} right now, huge momentum",
    "{a} looking strong today, {a} to the moon",
    "just aped into {a}, {a} breakout incoming",
    "{a} volume spiking, watch {a} closely",
    "not sure about {a}, might dump",
]


class MockCollector:
    source_name = "mock"

    def __init__(
        self,
        cfg: MockSource,
        universe: UniverseConfig,
        signals: SignalsConfig | None = None,
    ):
        self.cfg = cfg
        self.universe = universe
        self.signals = signals if signals is not None else get_signals()
        self._tickers = list(universe.symbols.keys())
        # Randomly pick a "hot" ticker each cycle to create bursts.
        self._rng = random.Random()

    def collect(self) -> list[SocialEvent]:
        if not self._tickers:
            return []
        events: list[SocialEvent] = []
        now = utcnow()
        hot = self._rng.choice(self._tickers)
        for _ in range(self.cfg.events_per_poll):
            # 60% chance the mention is about the hot ticker -> creates a spike.
            ticker = hot if self._rng.random() < 0.6 else self._rng.choice(self._tickers)
            spec = self.universe.symbols[ticker]
            alias = self._rng.choice(spec.aliases or [ticker.lower()])
            text = self._rng.choice(_TEMPLATES).format(a=f"${alias.lstrip('$')}")
            events.append(
                SocialEvent(
                    source=self.source_name,
                    external_id=uuid.uuid4().hex,
                    ticker=ticker,
                    author=f"user{self._rng.randint(1, 9999)}",
                    text=text,
                    url="",
                    weight=1.0,
                    sentiment=sentiment_score(text, self.signals),
                    text_hash=text_fingerprint(text),
                    created_at=now - timedelta(seconds=self._rng.randint(0, 60)),
                )
            )
        log.debug("mock produced %d events (hot=%s)", len(events), hot)
        return events
