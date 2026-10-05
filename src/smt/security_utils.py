"""Helpers that keep secrets out of logs and operator-facing output."""

from __future__ import annotations

import re

from sqlalchemy.engine import make_url

# scheme://user:pass@host — password is the run of characters between the
# first : after the scheme and the @ that starts the host.
_PASSWORD_IN_URL = re.compile(r"(://[^:/?#]+:)([^@]*)(@)")


def mask_database_url(url: str) -> str:
    """Return a DSN safe to log: the password is replaced with ``***``.

    Uses SQLAlchemy's ``hide_password`` rendering when the URL parses, and a
    regex fallback otherwise. Never raises. SQLite URLs are returned unchanged
    so forms like ``sqlite:///:memory:`` are not rewritten.
    """
    try:
        text = url if isinstance(url, str) else str(url)
    except Exception:
        return "***"
    if not text:
        return text
    try:
        if text.lower().startswith("sqlite"):
            return text
        return make_url(text).render_as_string(hide_password=True)
    except Exception:
        try:
            return _PASSWORD_IN_URL.sub(r"\1***\3", text)
        except Exception:
            return text


def mask_database_urls_in_text(text: str, url: str | None = None) -> str:
    """Mask DSN passwords inside free-form log or exception text. Never raises."""
    try:
        out = text if isinstance(text, str) else str(text)
        if url:
            needle = url if isinstance(url, str) else str(url)
            out = out.replace(needle, mask_database_url(url))
        return _PASSWORD_IN_URL.sub(r"\1***\3", out)
    except Exception:
        return "***"
