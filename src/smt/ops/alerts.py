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

    def notify(self, subject: str, body: str, critical: bool = False) -> bool:
        """Send to all configured channels.

        Email and Telegram receive everything; ntfy is reserved for critical
        events so the urgent-priority push stays meaningful.

        Returns True only when every configured channel accepted the message.
        Callers that must not advance state on a failed delivery (e.g. the
        weekly report scheduler) should gate on this return value.
        """
        log.info("ALERT%s: %s | %s", " [CRITICAL]" if critical else "", subject, body)

        channels = 0
        ok = True

        if self.s.smtp_host and self.s.alert_email_to:
            channels += 1
            ok = self._email(subject, body) and ok

        if self.s.telegram_bot_token and self.s.telegram_chat_id:
            channels += 1
            ok = self._telegram(subject, body) and ok

        if critical and self.s.ntfy_topic_url:
            channels += 1
            ok = self._ntfy(subject, body) and ok

        return ok if channels > 0 else False

    # ---- channels ----------------------------------------------------------

    def _email(self, subject: str, body: str) -> bool:
        if not (self.s.smtp_host and self.s.alert_email_to):
            return True
        try:
            msg = MIMEText(body)
            msg["Subject"] = f"[smt] {subject}"
            msg["From"] = self.s.smtp_user or "smt@localhost"
            msg["To"] = self.s.alert_email_to
            with smtplib.SMTP(self.s.smtp_host, self.smtp_port, timeout=15) as server:
                server.starttls()
                if self.s.smtp_user:
                    server.login(self.s.smtp_user, self.smtp_password)
                server.send_message(msg)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("email alert failed: %s", exc)
            return False

    def _ntfy(self, subject: str, body: str) -> bool:
        if not self.s.ntfy_topic_url:
            return True
        try:
            resp = httpx.post(
                self.s.ntfy_topic_url,
                data=body.encode("utf-8"),
                headers={"Title": subject, "Priority": "urgent"},
                timeout=15,
            )
            if resp.status_code >= 400:
                log.warning("ntfy alert rejected (HTTP %s)", resp.status_code)
                return False
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy alert failed: %s", exc)
            return False

    def _telegram(self, subject: str, body: str) -> bool:
        if not (self.s.telegram_bot_token and self.s.telegram_chat_id):
            return True
        url = f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage"
        ok = True
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
                    ok = False
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram alert failed (part %d): %s", i + 1, exc)
                ok = False
        return ok
