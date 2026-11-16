"""Water-sample ingestion service with optimistic concurrency and idempotency.

Semantics
---------
Every sample has a ``version``. A client uploads a sample together with the
version it edited from (``base_version``).

* ``base_version == 0``  -> create if absent, otherwise 409.
* ``base_version == server version`` -> update, bump version.
* ``base_version != server version`` -> 409 with both local and server content.

Every mutating request carries a client-generated ``request_id``. The first
response for a request id is stored and replayed verbatim, so a lost response
can be retried without a duplicate write or a spurious conflict. A replay is
only accepted for the same sample and identical payload; reusing a request id
for different content is rejected.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .db import connect
from .models import MAX_VERSION, payload_hash


class ConflictError(Exception):
    def __init__(self, local: dict[str, Any], server: dict[str, Any] | None) -> None:
        super().__init__("version conflict")
        self.local = local
        self.server = server


class RequestReuseError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_snapshot(
    sample_no: str, station_no: str, sampled_at: str, ph: float, version: int
) -> dict[str, Any]:
    return {
        "sample_no": sample_no,
        "station_no": station_no,
        "sampled_at": sampled_at,
        "ph": ph,
        "version": version,
    }


def row_to_sample(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "sample_no": row["sample_no"],
        "station_no": row["station_no"],
        "sampled_at": row["sampled_at"],
        "ph": row["ph"],
        "version": row["version"],
        "updated_at": row["updated_at"],
    }


def get_sample(database_path: str, sample_no: str) -> dict[str, Any] | None:
    with connect(database_path) as conn:
        row = conn.execute(
            "SELECT sample_no, station_no, sampled_at, ph, version, updated_at "
            "FROM samples WHERE sample_no = ?",
            (sample_no,),
        ).fetchone()
    return row_to_sample(row)


def list_samples(database_path: str) -> list[dict[str, Any]]:
    with connect(database_path) as conn:
        rows = conn.execute(
            "SELECT sample_no, station_no, sampled_at, ph, version, updated_at "
            "FROM samples ORDER BY sample_no"
        ).fetchall()
    return [row_to_sample(row) for row in rows if row is not None]


def _replay(conn: sqlite3.Connection, request_id: str, payload_fingerprint: str):
    row = conn.execute(
        "SELECT sample_no, payload_hash, response_status, response_body "
        "FROM idempotency WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None:
        return None
    if row["payload_hash"] != payload_fingerprint:
        raise RequestReuseError("request_id was already used for a different payload")
    return row["response_status"], json.loads(row["response_body"])


def _remember(
    conn: sqlite3.Connection,
    request_id: str,
    sample_no: str,
    payload_fingerprint: str,
    status: int,
    body: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO idempotency (request_id, sample_no, payload_hash, "
        "response_status, response_body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            request_id,
            sample_no,
            payload_fingerprint,
            status,
            json.dumps(body, sort_keys=True, separators=(",", ":")),
            _now(),
        ),
    )


def upsert_sample(
    database_path: str,
    *,
    sample_no: str,
    station_no: str,
    sampled_at: str,
    ph: float,
    base_version: int,
    request_id: str,
) -> tuple[int, dict[str, Any], bool]:
    """Create or update one sample atomically.

    Returns ``(http_status, body, replayed)``.
    """
    fingerprint = payload_hash(
        {
            "sample_no": sample_no,
            "station_no": station_no,
            "sampled_at": sampled_at,
            "ph": ph,
            "base_version": base_version,
        }
    )
    with connect(database_path) as conn:
        # Serialise writers; SQLite has no row-level locks.
        conn.execute("BEGIN IMMEDIATE")
        replay = _replay(conn, request_id, fingerprint)
        if replay is not None:
            status, body = replay
            conn.execute("COMMIT")
            return status, body, True

        row = conn.execute(
            "SELECT sample_no, station_no, sampled_at, ph, version, updated_at "
            "FROM samples WHERE sample_no = ?",
            (sample_no,),
        ).fetchone()
        existing = row_to_sample(row)

        local = _local_snapshot(sample_no, station_no, sampled_at, ph, base_version)

        if existing is None:
            if base_version != 0:
                conn.execute("ROLLBACK")
                raise ConflictError(local=local, server=None)
            now = _now()
            conn.execute(
                "INSERT INTO samples "
                "(sample_no, station_no, sampled_at, ph, version, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (sample_no, station_no, sampled_at, ph, now),
            )
            saved = get_sample_in_conn(conn, sample_no)
            body = {"outcome": "created", "sample": saved}
            _remember(conn, request_id, sample_no, fingerprint, 201, body)
            conn.execute("COMMIT")
            return 201, body, False

        if base_version != existing["version"]:
            conn.execute("ROLLBACK")
            raise ConflictError(local=local, server=existing)

        if existing["version"] >= MAX_VERSION:
            # Never wrap a version number; force a refresh instead.
            conn.execute("ROLLBACK")
            raise ConflictError(local=local, server=existing)

        now = _now()
        conn.execute(
            "UPDATE samples SET station_no = ?, sampled_at = ?, ph = ?, "
            "version = version + 1, updated_at = ? WHERE sample_no = ? AND version = ?",
            (station_no, sampled_at, ph, now, sample_no, base_version),
        )
        saved = get_sample_in_conn(conn, sample_no)
        if saved is None or saved["version"] != base_version + 1:
            # Defensive: BEGIN IMMEDIATE should make this unreachable.
            conn.execute("ROLLBACK")
            raise ConflictError(local=local, server=existing)
        body = {"outcome": "updated", "sample": saved}
        _remember(conn, request_id, sample_no, fingerprint, 200, body)
        conn.execute("COMMIT")
        return 200, body, False


def get_sample_in_conn(
    conn: sqlite3.Connection, sample_no: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT sample_no, station_no, sampled_at, ph, version, updated_at "
        "FROM samples WHERE sample_no = ?",
        (sample_no,),
    ).fetchone()
    return row_to_sample(row)
