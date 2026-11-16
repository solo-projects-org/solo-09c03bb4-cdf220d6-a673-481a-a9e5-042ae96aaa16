"""Water-sample ingestion service with optimistic concurrency, idempotency and
restore-safe epochs.

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

Warehouse epochs
----------------
A backup restore replaces every sample at once and increments the warehouse
``epoch`` (with a fresh opaque ``generation``). Clients name the epoch they
edited against (``base_epoch``). An upload whose epoch is not the live one is
refused with :class:`RestoreConflictError` *before* versions are compared, so a
request prepared against the pre-restore data can never write into the restored
data set merely because sample number and version happen to match. Idempotency
responses are namespaced by epoch as well, so replaying a pre-restore request
after a restore cannot report a successful write against the restored data.

Reads take their sample list and metadata from one transaction. Restores are a
single ``BEGIN IMMEDIATE`` transaction that deletes and re-inserts everything,
so a reader sees either the complete old warehouse or the complete restored one,
and a failure or process exit leaves the old warehouse untouched.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import UTC, datetime
from typing import Any

from .backup import build_backup, now_utc, parse_backup
from .db import INITIAL_EPOCH, connect
from .models import MAX_EPOCH, MAX_VERSION, payload_hash


class ConflictError(Exception):
    def __init__(
        self,
        local: dict[str, Any],
        server: dict[str, Any] | None,
        warehouse: dict[str, Any] | None = None,
    ) -> None:
        super().__init__("version conflict")
        self.local = local
        self.server = server
        self.warehouse = warehouse or {}


class RestoreConflictError(Exception):
    """The client edited against a different warehouse epoch (a restore ran)."""

    def __init__(
        self,
        *,
        local: dict[str, Any],
        base_epoch: int,
        warehouse: dict[str, Any],
    ) -> None:
        super().__init__("warehouse was restored")
        self.local = local
        self.base_epoch = base_epoch
        self.warehouse = warehouse


class RequestReuseError(Exception):
    pass


class RestoreLimitError(Exception):
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


SAMPLE_COLUMNS = (
    "SELECT sample_no, station_no, sampled_at, ph, version, updated_at FROM samples"
)


def _meta_in_conn(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    values = {row["key"]: row["value"] for row in rows}
    epoch = int(values.get("epoch", INITIAL_EPOCH))
    return {
        "site_id": values.get("site_id", "river-site"),
        "epoch": epoch,
        "generation": values.get("generation", f"epoch-{epoch}"),
        "restored_at": values.get("restored_at", ""),
        "restore_count": int(values.get("restore_count", "0")),
        "last_restore": values.get("last_restore", ""),
    }


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_meta(database_path: str) -> dict[str, Any]:
    with connect(database_path) as conn:
        return _meta_in_conn(conn)


def _epoch_accepts(meta: dict[str, Any], base_epoch: int) -> bool:
    """Is ``base_epoch`` allowed to write to this warehouse state?

    ``0`` means "client never saw a warehouse epoch". That is only accepted
    while the warehouse has never been restored, which keeps records written by
    builds that predate epochs usable. After the first restore only the live
    epoch is accepted.
    """
    if base_epoch == meta["epoch"]:
        return True
    return base_epoch == 0 and meta["restore_count"] == 0


def get_sample(database_path: str, sample_no: str) -> dict[str, Any] | None:
    with connect(database_path) as conn:
        conn.execute("BEGIN")
        row = conn.execute(
            SAMPLE_COLUMNS + " WHERE sample_no = ?",
            (sample_no,),
        ).fetchone()
        conn.execute("COMMIT")
    return row_to_sample(row)


def list_samples(database_path: str) -> list[dict[str, Any]]:
    return read_warehouse(database_path)["samples"]


def read_warehouse(database_path: str) -> dict[str, Any]:
    """All samples plus the epoch they belong to, from one read snapshot."""
    with connect(database_path) as conn:
        # A single deferred transaction is a consistent snapshot in WAL mode.
        conn.execute("BEGIN")
        meta = _meta_in_conn(conn)
        rows = conn.execute(SAMPLE_COLUMNS + " ORDER BY sample_no").fetchall()
        conn.execute("COMMIT")
    return {
        "warehouse": {
            "site_id": meta["site_id"],
            "epoch": meta["epoch"],
            "generation": meta["generation"],
            "restored_at": meta["restored_at"],
            "restore_count": meta["restore_count"],
        },
        "samples": [row_to_sample(row) for row in rows if row is not None],
    }


def export_backup(database_path: str) -> dict[str, Any]:
    """Download a backup from one consistent read snapshot."""
    snapshot = read_warehouse(database_path)
    meta = snapshot["warehouse"]
    return build_backup(
        site_id=meta["site_id"],
        epoch=meta["epoch"],
        restore_count=meta["restore_count"],
        samples=snapshot["samples"],
    )


def restore_backup(database_path: str, raw: bytes) -> dict[str, Any]:
    """Validate a backup and swap the whole warehouse for it atomically.

    The document is parsed and checked completely before a write transaction is
    opened. Any error leaves the live warehouse exactly as it was.
    """
    with connect(database_path) as conn:
        current = _meta_in_conn(conn)
    # Validated wholly in memory: BackupError reaches the caller before any
    # write transaction is opened, so the live warehouse is untouched.
    parsed = parse_backup(raw, expected_site_id=current["site_id"])

    next_epoch = current["epoch"] + 1
    if next_epoch > MAX_EPOCH:
        raise RestoreLimitError("warehouse epoch is exhausted")

    generation = secrets.token_hex(16)
    restored_at = now_utc()
    summary = {
        "site_id": current["site_id"],
        "epoch": next_epoch,
        "generation": generation,
        "restored_at": restored_at,
        "restore_count": current["restore_count"] + 1,
        "sample_count": len(parsed["samples"]),
    }
    with connect(database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Re-read under the write lock: another restore may have finished
            # between validation and here.
            locked = _meta_in_conn(conn)
            next_epoch = locked["epoch"] + 1
            if next_epoch > MAX_EPOCH:
                raise RestoreLimitError("warehouse epoch is exhausted")
            generation = secrets.token_hex(16)
            restored_at = now_utc()
            summary.update(
                epoch=next_epoch,
                generation=generation,
                restored_at=restored_at,
                restore_count=locked["restore_count"] + 1,
            )
            conn.execute("DELETE FROM samples")
            conn.executemany(
                "INSERT INTO samples "
                "(sample_no, station_no, sampled_at, ph, version, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        sample["sample_no"],
                        sample["station_no"],
                        sample["sampled_at"],
                        sample["ph"],
                        sample["version"],
                        sample["updated_at"],
                    )
                    for sample in parsed["samples"]
                ],
            )
            _set_meta(conn, "epoch", str(next_epoch))
            _set_meta(conn, "generation", generation)
            _set_meta(conn, "restored_at", restored_at)
            _set_meta(conn, "restore_count", str(summary["restore_count"]))
            _set_meta(conn, "last_restore", restored_at)
            # Drop pre-restore idempotency receipts. Their (epoch, request_id)
            # key already isolates them; deleting keeps the table bounded and
            # guarantees no old response can ever be replayed.
            conn.execute("DELETE FROM idempotency WHERE epoch <> ?", (str(next_epoch),))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return summary


def _replay(
    conn: sqlite3.Connection, epoch: str, request_id: str, payload_fingerprint: str
):
    # Receipts are namespaced by epoch; a restore deletes pre-restore receipts,
    # so an old request can never be replayed as a post-restore success.
    row = conn.execute(
        "SELECT sample_no, payload_hash, response_status, response_body "
        "FROM idempotency WHERE epoch = ? AND request_id = ?",
        (epoch, request_id),
    ).fetchone()
    if row is None:
        return None
    if row["payload_hash"] != payload_fingerprint:
        raise RequestReuseError("request_id was already used for a different payload")
    return row["response_status"], json.loads(row["response_body"])


def _remember(
    conn: sqlite3.Connection,
    epoch: str,
    request_id: str,
    sample_no: str,
    payload_fingerprint: str,
    status: int,
    body: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO idempotency (request_id, epoch, sample_no, payload_hash, "
        "response_status, response_body, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            request_id,
            epoch,
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
    base_epoch: int = 0,
    generation: str = "",
) -> tuple[int, dict[str, Any], bool]:
    """Create or update one sample atomically.

    Returns ``(http_status, body, replayed)``.
    """
    # The fingerprint intentionally matches the pre-epoch build. A request
    # queued by an older page must still replay after an upgrade; the epoch is
    # enforced separately (and pre-restore receipts are purged by a restore).
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
        # Serialise writers; SQLite has no row-level locks. A restore either
        # finished before this transaction (we see the new epoch) or waits for
        # it, so every upload belongs unambiguously to one side of the switch.
        conn.execute("BEGIN IMMEDIATE")
        meta = _meta_in_conn(conn)
        epoch = str(meta["epoch"])
        warehouse = {
            "site_id": meta["site_id"],
            "epoch": meta["epoch"],
            "generation": meta["generation"],
            "restored_at": meta["restored_at"],
            "restore_count": meta["restore_count"],
        }
        local = _local_snapshot(sample_no, station_no, sampled_at, ph, base_version)

        if not _epoch_accepts(meta, base_epoch):
            conn.execute("ROLLBACK")
            raise RestoreConflictError(
                local=local, base_epoch=base_epoch, warehouse=warehouse
            )
        if (
            generation
            and base_epoch == meta["epoch"]
            and generation != meta["generation"]
        ):
            # Same epoch but a different restore generation: refuse rather than
            # guess which side of an identity swap the request belongs to.
            conn.execute("ROLLBACK")
            raise RestoreConflictError(
                local=local, base_epoch=base_epoch, warehouse=warehouse
            )

        replay = _replay(conn, epoch, request_id, fingerprint)
        if replay is not None:
            status, body = replay
            conn.execute("COMMIT")
            return status, body, True

        row = conn.execute(
            SAMPLE_COLUMNS + " WHERE sample_no = ?", (sample_no,)
        ).fetchone()
        existing = row_to_sample(row)

        if existing is None:
            if base_version != 0:
                conn.execute("ROLLBACK")
                raise ConflictError(local=local, server=None, warehouse=warehouse)
            now = _now()
            conn.execute(
                "INSERT INTO samples "
                "(sample_no, station_no, sampled_at, ph, version, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (sample_no, station_no, sampled_at, ph, now),
            )
            saved = get_sample_in_conn(conn, sample_no)
            body = {"outcome": "created", "sample": saved, "warehouse": warehouse}
            _remember(conn, epoch, request_id, sample_no, fingerprint, 201, body)
            conn.execute("COMMIT")
            return 201, body, False

        if base_version != existing["version"]:
            conn.execute("ROLLBACK")
            raise ConflictError(local=local, server=existing, warehouse=warehouse)

        if existing["version"] >= MAX_VERSION:
            # Never wrap a version number; force a refresh instead.
            conn.execute("ROLLBACK")
            raise ConflictError(local=local, server=existing, warehouse=warehouse)

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
            raise ConflictError(local=local, server=existing, warehouse=warehouse)
        body = {"outcome": "updated", "sample": saved, "warehouse": warehouse}
        _remember(conn, epoch, request_id, sample_no, fingerprint, 200, body)
        conn.execute("COMMIT")
        return 200, body, False


def get_sample_in_conn(
    conn: sqlite3.Connection, sample_no: str
) -> dict[str, Any] | None:
    row = conn.execute(SAMPLE_COLUMNS + " WHERE sample_no = ?", (sample_no,)).fetchone()
    return row_to_sample(row)
