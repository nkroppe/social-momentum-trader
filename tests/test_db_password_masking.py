"""Sentinel tests: database passwords never appear in logs or doctor output."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy import create_engine as sa_create_engine

from smt.config import Settings
from smt.ops.preflight import run_preflight
from smt.security_utils import mask_database_url
from smt.store import Store

SENTINEL = "S3ntinel-pw-9f2c1"
SENTINEL_URL = f"postgresql+psycopg://smt:{SENTINEL}@localhost:5432/smt"


def test_mask_database_url_postgres_hides_password():
    masked = mask_database_url(SENTINEL_URL)
    assert SENTINEL not in masked
    assert "***" in masked
    assert "smt" in masked
    assert "localhost:5432" in masked
    assert "smt" in masked.split("/")[-1]


def test_mask_database_url_sqlite_passthrough():
    sqlite_url = "sqlite:///./data/smt.sqlite"
    assert mask_database_url(sqlite_url) == sqlite_url
    memory = "sqlite:///:memory:"
    assert mask_database_url(memory) == memory


def test_mask_database_url_garbage_never_raises():
    for garbage in ("not a url", "???", "", "::::"):
        masked = mask_database_url(garbage)
        assert isinstance(masked, str)


def test_doctor_postgres_database_url_masks_sentinel(monkeypatch, tmp_path):
    settings = Settings(database_url=SENTINEL_URL)
    monkeypatch.setattr("smt.ops.preflight.get_settings", lambda: settings)
    monkeypatch.setattr("smt.ops.preflight._market_data_checks", lambda: [])
    monkeypatch.chdir(tmp_path)

    results = run_preflight("production")
    blob = "\n".join(f"{row.name}: {row.detail}" for row in results)
    assert SENTINEL not in blob

    check = {row.name: row for row in results}["postgres_database_url"]
    assert check.passed is True
    assert SENTINEL not in check.detail
    assert "***" in check.detail


def test_store_database_ready_log_masks_sentinel(monkeypatch, caplog):
    def _sqlite_engine(_url, **kwargs):
        return sa_create_engine("sqlite:///:memory:", **kwargs)

    monkeypatch.setattr("smt.store.create_engine", _sqlite_engine)

    with caplog.at_level(logging.INFO, logger="smt.store"):
        store = Store(SENTINEL_URL)
        store.init_db()

    assert store.database_url == SENTINEL_URL
    ready = [rec.getMessage() for rec in caplog.records if "Database ready" in rec.getMessage()]
    assert ready
    assert SENTINEL not in caplog.text
    assert all(SENTINEL not in line for line in ready)
    assert "***" in ready[0]


def test_setup_logging_uses_rotating_file_handler(tmp_path, monkeypatch):
    from smt import logging_setup

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    monkeypatch.setattr(logging_setup, "_CONFIGURED", False)
    for handler in saved_handlers:
        root.removeHandler(handler)
    try:
        logging_setup.setup_logging(log_dir=str(tmp_path))
        rotating = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rotating) == 1
        handler = rotating[0]
        assert handler.maxBytes == 50 * 1024 * 1024
        assert handler.backupCount == 5
        assert Path(handler.baseFilename) == (tmp_path / "smt.log").resolve()
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            handler.close()
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        logging_setup._CONFIGURED = True
