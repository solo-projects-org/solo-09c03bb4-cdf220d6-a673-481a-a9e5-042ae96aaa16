"""SQLite access helpers for the sampling data warehouse.

The warehouse has a *generation* (also called data era).  Every restore from a
backup bumps the generation and mints a fresh random ``epoch``.  Sample rows do
not carry the epoch: the whole database is one era at a time.  Idempotency
records do carry the epoch, so an old request retried after a restore can never
replay a pre-restore success.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    created_at       TEXT NOT NULL,
    epoch            TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS restore_log (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    generation        INTEGER NOT NULL,
    epoch             TEXT NOT NULL,
    restored_at       TEXT NOT NULL,
    sample_count      INTEGER NOT NULL,
    source_epoch      TEXT,
    source_generation INTEGER,
    backup_checksum   TEXT NOT NULL
);
"""


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_epoch() -> str:
    return uuid.uuid4().hex


def get_meta_in_conn(conn: sqlite3.Connection) -> dict[str, Any]:
    """Read the warehouse metadata inside the caller's transaction/snapshot."""
    values = {
        row["key"]: row["value"]
        for row in conn.execute("SELECT key, value FROM meta").fetchall()
    }
    return {
        "epoch": values.get("epoch", ""),
        "generation": int(values.get("generation", "1")),
        "site_id": values.get("site_id", ""),
        "created_at": values.get("created_at"),
        "restored_at": values.get("restored_at"),
    }


def init_db(database_path: str, site_id: str) -> None:
    """Create/migrate the database and make sure it has an epoch identity.

    Existing databases (created before backups existed) are assigned
    generation 1.  Because no restore has ever happened, their idempotency
    rows are safe to stamp with the current epoch.
    """
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)

        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(idempotency)")
        }
        if "epoch" not in columns:
            conn.execute("ALTER TABLE idempotency ADD COLUMN epoch TEXT")

        existing = get_meta_in_conn(conn)
        if not existing["epoch"]:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('epoch', ?)",
                (new_epoch(),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('generation', '1')"
            )
        if not existing["site_id"]:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('site_id', ?)",
                (site_id,),
            )
        if not existing["created_at"]:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('created_at', ?)",
                (utc_now(),),
            )

        meta = get_meta_in_conn(conn)
        if meta["generation"] == 1:
            # Legacy rows were written into this very era: safe to label them.
            conn.execute(
                "UPDATE idempotency SET epoch = ? WHERE epoch IS NULL",
                (meta["epoch"],),
            )
        else:  # pragma: no cover - defensive for hand-edited databases
            conn.execute("DELETE FROM idempotency WHERE epoch IS NULL")


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
