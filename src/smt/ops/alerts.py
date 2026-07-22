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


class Alerter:
    def __init__(self, settings: Settings):
        self.s = settings

    def notify(self, subject: str, body: str, critical: bool = False) -> None:
        """Send to all configured channels. Critical events also push to phone."""
        log.info("ALERT%s: %s | %s", " [CRITICAL]" if critical else "", subject, body)
        self._email(subject, body)
        if critical:
            self._ntfy(subject, body)
            self._telegram(subject, body)

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
        try:
            httpx.post(
                f"https://api.telegram.org/bot{self.s.telegram_bot_token}/sendMessage",
                json={"chat_id": self.s.telegram_chat_id, "text": f"{subject}\n\n{body}"},
                timeout=15,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram alert failed: %s", exc)
