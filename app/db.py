"""SQLite access helpers for the sampling data warehouse.

The database carries a monotonically increasing *epoch*. Every restore replaces
all samples and bumps the epoch in one transaction. Clients must name the epoch
they edited against, so a request prepared before a restore can never be
applied to the restored data by accident.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 2
DEFAULT_SITE_ID = "river-site"
INITIAL_EPOCH = 1

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
    request_id       TEXT NOT NULL,
    epoch            TEXT NOT NULL,
    sample_no        TEXT NOT NULL,
    payload_hash     TEXT NOT NULL,
    response_status  INTEGER NOT NULL,
    response_body    TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    PRIMARY KEY (epoch, request_id)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _migrate_idempotency(conn: sqlite3.Connection) -> None:
    """Give pre-epoch databases the (epoch, request_id) namespace.

    Rows written by the old build are copied under epoch 0. That epoch can never
    match a live epoch, so an in-flight request from the old page cannot replay
    a pre-upgrade response as if it belonged to the current data set.
    """
    if "epoch" in _table_columns(conn, "idempotency"):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Re-check under the write lock: another process may have migrated
        # between the cheap check above and this transaction.
        if "epoch" in _table_columns(conn, "idempotency"):
            conn.execute("COMMIT")
            return
        conn.execute("ALTER TABLE idempotency RENAME TO idempotency_legacy")
        conn.execute(
            """
            CREATE TABLE idempotency (
                request_id       TEXT NOT NULL,
                epoch            TEXT NOT NULL,
                sample_no        TEXT NOT NULL,
                payload_hash     TEXT NOT NULL,
                response_status  INTEGER NOT NULL,
                response_body    TEXT NOT NULL,
                created_at       TEXT NOT NULL,
                PRIMARY KEY (epoch, request_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO idempotency
                (request_id, epoch, sample_no, payload_hash,
                 response_status, response_body, created_at)
            SELECT request_id, '0', sample_no, payload_hash,
                   response_status, response_body, created_at
            FROM idempotency_legacy
            """
        )
        conn.execute("DROP TABLE idempotency_legacy")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def init_db(database_path: str, site_id: str | None = None) -> None:
    """Create the database file, tables, meta and indexes if missing."""
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(database_path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        _migrate_idempotency(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('epoch', ?)",
                (str(INITIAL_EPOCH),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('site_id', ?)",
                (site_id or DEFAULT_SITE_ID,),
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('restored_at', '')"
            )
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES ('restore_count', '0')"
            )
            # Receipts migrated from the pre-epoch schema are stored under
            # epoch '0'. As long as no restore ever ran, the warehouse content
            # is still the one those receipts describe, so adopt them into the
            # live epoch. After a restore they stay muted (and are deleted).
            restore_count = int(
                conn.execute(
                    "SELECT value FROM meta WHERE key = 'restore_count'"
                ).fetchone()[0]
            )
            epoch = conn.execute(
                "SELECT value FROM meta WHERE key = 'epoch'"
            ).fetchone()[0]
            if restore_count == 0:
                conn.execute(
                    "UPDATE idempotency SET epoch = ? WHERE epoch = '0'", (epoch,)
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


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
