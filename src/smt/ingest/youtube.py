"""YouTube collector (official Data API). Requires the `live` extra.

Pulls recent videos for configured channels/queries and scans title +
description (and optionally transcript) for ticker mentions.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..config import Settings, UniverseConfig, YouTubeSource
from ..logging_setup import get_logger
from ..models import SocialEvent
from .base import extract_tickers

log = get_logger("smt.ingest.youtube")


class YouTubeCollector:
    source_name = "youtube"

    def __init__(self, settings: Settings, cfg: YouTubeSource, universe: UniverseConfig):
        from googleapiclient.discovery import build  # lazy import

        self.cfg = cfg
        self.universe = universe
        self.client = build("youtube", "v3", developerKey=settings.youtube_api_key)

    def _search(self, query: str, channel_id: str | None) -> list[dict]:
        params = {
            "part": "snippet",
            "type": "video",
            "order": "date",
            "maxResults": self.cfg.max_results_per_query,
        }
        if query:
            params["q"] = query
        if channel_id:
            params["channelId"] = channel_id
        return self.client.search().list(**params).execute().get("items", [])

    def _transcript_text(self, video_id: str) -> str:
        if not self.cfg.fetch_transcripts:
            return ""
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            chunks = YouTubeTranscriptApi.get_transcript(video_id)
            return " ".join(c["text"] for c in chunks)
        except Exception:  # noqa: BLE001
            return ""

    def collect(self) -> list[SocialEvent]:
        events: list[SocialEvent] = []
        searches = [(q, None) for q in self.cfg.queries]
        searches += [("", c) for c in self.cfg.channels]

        for query, channel in searches:
            try:
                for item in self._search(query, channel):
                    vid = item["id"]["videoId"]
                    snip = item["snippet"]
                    text = f"{snip.get('title', '')}\n{snip.get('description', '')}"
                    text += "\n" + self._transcript_text(vid)
                    created = datetime.fromisoformat(
                        snip["publishedAt"].replace("Z", "+00:00")
                    ).astimezone(UTC)
                    for ticker in extract_tickers(text, self.universe):
                        events.append(
                            SocialEvent(
                                source=self.source_name,
                                external_id=vid,
                                ticker=ticker,
                                author=snip.get("channelTitle", ""),
                                text=text[:2000],
                                url=f"https://youtube.com/watch?v={vid}",
                                weight=1.5,  # video mentions weighted a bit higher
                                created_at=created,
                            )
                        )
            except Exception as exc:  # noqa: BLE001
                log.warning("youtube poll failed (q=%r channel=%r): %s", query, channel, exc)
        return events
