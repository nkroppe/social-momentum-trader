"""Alerting: email digests + phone-reachable push for critical events.

Best-effort and non-fatal: alert delivery failures never crash the trader.
"""

from __future__ import annotations

import smtplib
from email.mime.text import MIMEText

import httpx

from ..config import Settings
from ..logging_setup import get_logger

log = get_logger("smt.alerts")

# Telegram rejects messages over 4096 characters outright, so a long report has
# to be split rather than truncated.
TELEGRAM_MAX_CHARS = 4096


def split_message(text: str, limit: int = TELEGRAM_MAX_CHARS) -> list[str]:
    """Split on line boundaries into chunks within `limit`.

    Keeps whole lines together so a trade row is never cut in half. A single
    line longer than the limit is hard-split as a last resort.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class Alerter:
    def __init__(self, settings: Settings):
        self.s = settings

    def notify(self, subject: str, body: str, critical: bool = False) -> None:
        """Send to all configured channels.

        Email and Telegram receive everything; ntfy is reserved for critical
        events so the urgent-priority push stays meaningful.
        """
        log.info("ALERT%s: %s | %s", " [CRITICAL]" if critical else "", subject, body)
        self._email(subject, body)
        self._telegram(subject, body)
        if critical:
            self._ntfy(subject, body)

    # ---- channels ----------------------------------------------------------

    def _email(self, subject: str, body: str) -> None:
        if not (self.s.smtp_host and self.s.alert_email_to):
            return
        try:
            msg = MIMEText(body)
            msg["Subject"] = f"[smt] {subject}"
            msg["From"] = self.s.smtp_user or "smt@localhost"
            msg["To"] = self.s.alert_email_to
            with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as server:
                server.starttls()
                if self.s.smtp_user:
                    server.login(self.s.smtp_user, self.s.smtp_password)
                server.send_message(msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("email alert failed: %s", exc)

    def _ntfy(self, subject: str, body: str) -> None:
        if not self.s.ntfy_topic_url:
            return
        try:
            httpx.post(
                self.s.ntfy_topic_url,
                data=body.encode("utf-8"),
                headers={"Title": subject, "Priority": "urgent"},
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy alert failed: %s", exc)

    def _telegram(self, subject: str, body: str) -> None:
        if not (self.s.telegram_bot_token and self.s.telegram_chat_id):
            return
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        for i, chunk in enumerate(split_message(f"{subject}\n\n{body}")):
            try:
                resp = httpx.post(
                    url,
                    json={"chat_id": self.s.telegram_chat_id, "text": chunk},
                    timeout=15,
                )
                if resp.status_code != 200:
                    # Surface the API's reason; a silent 400 here means the user
                    # simply never receives alerts.
                    log.warning(
                        "telegram alert rejected (HTTP %s): %s",
                        resp.status_code,
                        resp.text[:300],
                    )
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram alert failed (part %d): %s", i + 1, exc)
