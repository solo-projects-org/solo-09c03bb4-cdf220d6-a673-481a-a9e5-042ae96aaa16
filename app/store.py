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

Backups and restores
--------------------
A restore replaces the whole sample set and bumps the warehouse *generation*,
minting a new ``epoch``.  Idempotency records are epoch-scoped:

* a request id seen in the current epoch replays its stored response;
* a request id only known from a *previous* epoch is never replayed.  Its
  pre-restore success cannot be reported as a post-restore success, so the
  request is handled against the restored data instead.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .backup import build_backup
from .db import connect, get_meta_in_conn, new_epoch, utc_now
from .models import MAX_VERSION, payload_hash


class ConflictError(Exception):
    def __init__(self, local: dict[str, Any], server: dict[str, Any] | None) -> None:
        super().__init__("version conflict")
        self.local = local
        self.server = server


class RequestReuseError(Exception):
    pass


class InvalidRestore(Exception):
    """Raised when a validated backup cannot be applied (e.g. site mismatch)."""


class EpochMismatchError(Exception):
    """The request was issued against a different warehouse era."""

    def __init__(self, client_epoch: str, server_epoch: str) -> None:
        super().__init__("warehouse epoch changed")
        self.client_epoch = client_epoch
        self.server_epoch = server_epoch


class EpochRequiredError(Exception):
    """A write arrived without an era after the warehouse had been restored.

    Such a request may be based on pre-restore data, so it is refused instead
    of guessed at.  The response carries the current era so a current client
    can reconcile and re-submit deliberately.
    """

    def __init__(self, server_epoch: str, generation: int) -> None:
        super().__init__("warehouse epoch is required after a restore")
        self.server_epoch = server_epoch
        self.generation = generation


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


def snapshot_samples(database_path: str) -> dict[str, Any]:
    """Read metadata and samples from one consistent snapshot.

    Restores are single transactions, so a reader either sees the complete old
    sample set or the complete restored set -- never a mixture.
    """
    with connect(database_path) as conn:
        conn.execute("BEGIN")
        try:
            meta = get_meta_in_conn(conn)
            rows = conn.execute(
                "SELECT sample_no, station_no, sampled_at, ph, version, updated_at "
                "FROM samples ORDER BY sample_no"
            ).fetchall()
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    return {
        "samples": [row_to_sample(row) for row in rows if row is not None],
        "epoch": meta["epoch"],
        "generation": meta["generation"],
        "restored_at": meta.get("restored_at"),
    }


def get_warehouse_meta(database_path: str) -> dict[str, Any]:
    """Public identity/era of the warehouse, used by clients to detect restores."""
    with connect(database_path) as conn:
        # One read snapshot: epoch, generation and count must describe the
        # same instant, otherwise a restore could make them contradict.
        conn.execute("BEGIN")
        try:
            meta = get_meta_in_conn(conn)
            meta["sample_count"] = conn.execute(
                "SELECT COUNT(*) AS n FROM samples"
            ).fetchone()["n"]
            last = conn.execute(
                "SELECT generation, epoch, restored_at, sample_count, backup_checksum "
                "FROM restore_log ORDER BY id DESC LIMIT 1"
            ).fetchone()
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    meta["last_restore"] = dict(last) if last else None
    return meta


def export_backup(database_path: str, site_id: str) -> dict[str, Any]:
    """Snapshot all samples and metadata into a consistent backup document."""
    snapshot = snapshot_samples(database_path)
    meta = {
        "epoch": snapshot["epoch"],
        "generation": snapshot["generation"],
    }
    return build_backup(
        site_id=site_id,
        meta=meta,
        samples=snapshot["samples"],
        exported_at=utc_now(),
    )


def _replay(
    conn: sqlite3.Connection,
    request_id: str,
    payload_fingerprint: str,
    epoch: str,
):
    """Return a replayable response, or None if this request must run fresh.

    Records from other epochs are ignored (never replayed), which is what
    keeps a pre-restore receipt from confirming a post-restore write.  The
    payload check only applies within the current epoch.
    """
    row = conn.execute(
        "SELECT sample_no, payload_hash, response_status, response_body, epoch "
        "FROM idempotency WHERE request_id = ?",
        (request_id,),
    ).fetchone()
    if row is None or row["epoch"] != epoch:
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
    epoch: str,
) -> None:
    # The request id is globally unique.  Reusing it in a new epoch replaces
    # the dead pre-restore record so the table cannot grow stale entries.
    conn.execute(
        "INSERT INTO idempotency (request_id, sample_no, payload_hash, "
        "response_status, response_body, created_at, epoch) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(request_id) DO UPDATE SET "
        "sample_no = excluded.sample_no, payload_hash = excluded.payload_hash, "
        "response_status = excluded.response_status, "
        "response_body = excluded.response_body, created_at = excluded.created_at, "
        "epoch = excluded.epoch",
        (
            request_id,
            sample_no,
            payload_fingerprint,
            status,
            json.dumps(body, sort_keys=True, separators=(",", ":")),
            utc_now(),
            epoch,
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
    client_epoch: str | None = None,
) -> tuple[int, dict[str, Any], bool, str]:
    """Create or update one sample atomically.

    A caller that names a ``client_epoch`` different from the current warehouse
    epoch gets :class:`EpochMismatchError`; nothing is written.  Older clients
    that send no epoch are still accepted while the warehouse has never been
    restored (generation 1); after any restore an unversioned write gets
    :class:`EpochRequiredError` instead of being guessed at.

    Returns ``(http_status, body, replayed, committed_epoch)``.  The epoch is
    read inside the same transaction as the write, so the caller can report the
    era a response truly belongs to even if a restore races it.
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
        meta = get_meta_in_conn(conn)
        epoch = meta["epoch"]

        if client_epoch and client_epoch != epoch:
            conn.execute("ROLLBACK")
            raise EpochMismatchError(client_epoch, epoch)
        if not client_epoch and int(meta["generation"]) > 1:
            # After a restore an unversioned write cannot be shown to belong to
            # the current era. Refuse it explicitly rather than risk writing
            # pre-restore data over the restored database.
            conn.execute("ROLLBACK")
            raise EpochRequiredError(epoch, int(meta["generation"]))

        replay = _replay(conn, request_id, fingerprint, epoch)
        if replay is not None:
            status, body = replay
            conn.execute("COMMIT")
            return status, body, True, epoch

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
            now = utc_now()
            conn.execute(
                "INSERT INTO samples "
                "(sample_no, station_no, sampled_at, ph, version, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?)",
                (sample_no, station_no, sampled_at, ph, now),
            )
            saved = get_sample_in_conn(conn, sample_no)
            body = {"outcome": "created", "sample": saved}
            _remember(conn, request_id, sample_no, fingerprint, 201, body, epoch)
            conn.execute("COMMIT")
            return 201, body, False, epoch

        if base_version != existing["version"]:
            conn.execute("ROLLBACK")
            raise ConflictError(local=local, server=existing)

        if existing["version"] >= MAX_VERSION:
            # Never wrap a version number; force a refresh instead.
            conn.execute("ROLLBACK")
            raise ConflictError(local=local, server=existing)

        now = utc_now()
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
        _remember(conn, request_id, sample_no, fingerprint, 200, body, epoch)
        conn.execute("COMMIT")
        return 200, body, False, epoch


def get_sample_in_conn(
    conn: sqlite3.Connection, sample_no: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT sample_no, station_no, sampled_at, ph, version, updated_at "
        "FROM samples WHERE sample_no = ?",
        (sample_no,),
    ).fetchone()
    return row_to_sample(row)


def restore_backup(
    database_path: str,
    *,
    backup: dict[str, Any],
    site_id: str,
) -> dict[str, Any]:
    """Atomically replace every sample with the backup's contents.

    The write happens in one ``BEGIN IMMEDIATE`` transaction: either the whole
    restored set is visible or the old database is untouched.  A reader never
    observes a half-restored table, and a crash mid-way rolls back on the next
    open (SQLite journal).

    Every restore ends the previous era: the generation is bumped and a new
    epoch is minted, even when the uploaded file was exported from the very
    same era.  This is what gives the upload/restore interleaving a single
    unambiguous switch point.  Idempotency rows from the old era are deleted:
    the restored database must not let a pre-restore request replay its
    "success" any more, even if sample number and version look identical.
    """
    with connect(database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            meta = get_meta_in_conn(conn)
            if meta["site_id"] and meta["site_id"] != site_id:
                # The database itself belongs to another site: an operator has
                # pointed this instance at the wrong volume.
                raise InvalidRestore(
                    f"database belongs to site {meta['site_id']!r}, not {site_id!r}"
                )

            generation = int(meta["generation"]) + 1
            epoch = new_epoch()
            restored_at = utc_now()

            conn.execute("DELETE FROM samples")
            conn.executemany(
                "INSERT INTO samples "
                "(sample_no, station_no, sampled_at, ph, version, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        item["sample_no"],
                        item["station_no"],
                        item["sampled_at"],
                        item["ph"],
                        item["version"],
                        item["updated_at"],
                    )
                    for item in backup["samples"]
                ],
            )
            # Pre-restore receipts are dead: they must never replay an "ok"
            # for data that the restore replaced.
            conn.execute("DELETE FROM idempotency")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('generation', ?)",
                (str(generation),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('epoch', ?)",
                (epoch,),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('restored_at', ?)",
                (restored_at,),
            )
            conn.execute(
                "INSERT INTO restore_log (generation, epoch, restored_at, "
                "sample_count, source_epoch, source_generation, backup_checksum) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    generation,
                    epoch,
                    restored_at,
                    len(backup["samples"]),
                    backup["epoch"],
                    backup["generation"],
                    backup["checksum"],
                ),
            )
            # Triple-check the row count before letting the transaction stand.
            count = conn.execute("SELECT COUNT(*) AS n FROM samples").fetchone()["n"]
            if count != len(backup["samples"]):  # pragma: no cover - defensive
                raise InvalidRestore("restored row count mismatch")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    return {
        "outcome": "restored",
        "epoch": epoch,
        "generation": generation,
        "restored_at": restored_at,
        "sample_count": len(backup["samples"]),
    }
