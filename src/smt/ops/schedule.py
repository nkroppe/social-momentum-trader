"""Wall-clock weekly scheduling for the performance report.

The trading loop runs on monotonic intervals, which cannot express "Sunday at
8 PM Eastern": an interval drifts against the calendar and shifts by an hour
twice a year. This tracks the local wall clock in a configured IANA zone
instead, and persists the last send so a restart neither double-sends nor
silently skips a week.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..config import WeeklyReportConfig
from ..logging_setup import get_logger

log = get_logger("smt.schedule")


class WeeklyScheduler:
    def __init__(self, cfg: WeeklyReportConfig):
        self.cfg = cfg
        self.tz = self._load_zone(cfg.timezone)
        self.path = Path(cfg.state_file)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _load_zone(name: str):
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            # A missing tz database must not take the trader down; UTC still
            # produces a weekly report, just at an unexpected local hour.
            log.error("unknown timezone %r (%s); falling back to UTC", name, exc)
            return UTC

    # ---- Occurrences --------------------------------------------------------

    def _at(self, day: date) -> datetime:
        """The configured time-of-day on `day`, as local wall clock."""
        return datetime(
            day.year, day.month, day.day, self.cfg.hour, self.cfg.minute, tzinfo=self.tz
        )

    def previous_occurrence(self, now: datetime | None = None) -> datetime:
        """The most recent scheduled moment at or before `now`."""
        local = (now or datetime.now(UTC)).astimezone(self.tz)
        back = (local.weekday() - self.cfg.weekday_index) % 7
        candidate = self._at(local.date() - timedelta(days=back))
        if candidate > local:
            candidate = self._at(local.date() - timedelta(days=back + 7))
        return candidate

    def next_occurrence(self, now: datetime | None = None) -> datetime:
        # Stepping by calendar date rather than adding 7*24h keeps the wall
        # clock fixed across a daylight-saving transition.
        return self._at(self.previous_occurrence(now).date() + timedelta(days=7))

    @staticmethod
    def report_window(occurrence: datetime) -> tuple[datetime, datetime]:
        """The 7-day window a report covers, so consecutive weeks never overlap."""
        return occurrence - timedelta(days=7), occurrence

    # ---- Persisted state ----------------------------------------------------

    def last_sent(self) -> datetime | None:
        if not self.path.exists():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))["last_sent"]
            parsed = datetime.fromisoformat(raw)
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)

    def mark_sent(self, occurrence: datetime) -> None:
        self.path.write_text(
            json.dumps({"last_sent": occurrence.isoformat()}, indent=2), encoding="utf-8"
        )

    def ensure_initialized(self, now: datetime | None = None) -> None:
        """Treat the most recent occurrence as already sent.

        Without this a fresh deploy immediately fires a report for a week it has
        no data on. The first real report then lands at the next scheduled time.
        """
        if self.last_sent() is None:
            self.mark_sent(self.previous_occurrence(now))

    def due(self, now: datetime | None = None) -> datetime | None:
        """The occurrence to report on, or None when nothing is outstanding.

        A send missed during downtime is still delivered late rather than
        dropped, since a skipped week leaves a silent gap in the record.
        """
        if not self.cfg.enabled:
            return None
        occurrence = self.previous_occurrence(now)
        last = self.last_sent()
        if last is None or last < occurrence:
            return occurrence
        return None
