"""SQLite access helpers for the sampling data warehouse."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    sample_no   TEXT PRIMARY KEY,
    station_no  TEXT NOT NULL,
    sampled_at  TEXT NOT NULL,
    ph          REAL NOT NULL CHECK (ph >= 0.0 AND ph <= 14.0),
    version     INTEGER NOT NULL CHECK (version >= 1),
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency (
    request_id       TEXT PRIMARY KEY,
    sample_no        TEXT NOT NULL,
    payload_hash     TEXT NOT NULL,
    response_status  INTEGER NOT NULL,
    response_body    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
"""


def init_db(database_path: str) -> None:
    """Create the database file, tables and indexes if they do not exist."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


@contextmanager
def connect(database_path: str) -> Iterator[sqlite3.Connection]:
    """Open a short-lived connection with a bounded lock wait."""
    conn = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
        if conn.in_transaction:
            conn.rollback()
    finally:
        conn.close()
