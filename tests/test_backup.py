"""Tests for backup export, strict validation, restore and era semantics."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backup import checksum_of
from app.main import create_app

SITE = "test-site"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def client(db_path: Path):
    app = create_app(str(db_path), site_id=SITE)
    with TestClient(app) as test_client:
        yield test_client


def payload(**overrides):
    base = {
        "station_no": "ST-04",
        "sampled_at": "2026-11-16T08:30:00Z",
        "ph": 7.2,
        "base_version": 0,
        "request_id": "req-0000-0001",
    }
    base.update(overrides)
    return base


def create(client, sample_no="RIVER-0001", epoch=None, **overrides):
    body = payload(**overrides)
    if epoch:
        body["epoch"] = epoch
    response = client.put(f"/api/samples/{sample_no}", json=body)
    assert response.status_code == 201, response.text
    return response.json()["sample"]


def meta(client):
    return client.get("/api/warehouse").json()


def backup_doc(client) -> dict:
    response = client.get("/api/backup")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    return response.json()


def download(client) -> bytes:
    return client.get("/api/backup").content


def upload(client, doc, raw: bytes | None = None):
    content = raw if raw is not None else json.dumps(doc).encode()
    return client.post(
        "/api/backup/restore",
        content=content,
        headers={"Content-Type": "application/json"},
    )


def row_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]


# ------------------------------------------------------------------ export


def test_backup_contains_samples_identity_and_checksum(client):
    create(client, "RIVER-0001")
    create(client, "RIVER-0002", request_id="req-0000-0002")
    doc = backup_doc(client)
    assert doc["format"] == "river-sampling-backup"
    assert doc["format_version"] == 1
    assert doc["site_id"] == SITE
    assert doc["sample_count"] == 2
    assert {s["sample_no"] for s in doc["samples"]} == {"RIVER-0001", "RIVER-0002"}
    assert doc["checksum"] == checksum_of(doc)
    assert doc["epoch"] == meta(client)["epoch"]
    assert doc["generation"] == meta(client)["generation"]

    response = client.get("/api/backup")
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-backup-generation"] == str(doc["generation"])


# -------------------------------------------------------------- validation


INVALID_CASES = {
    "not_json": b"{not json",
    "not_object": b'["x"]',
    "wrong_format": json.dumps({"format": "something-else"}).encode(),
    "wrong_site": json.dumps(
        {
            "format": "river-sampling-backup",
            "format_version": 1,
            "site_id": "other-site",
            "epoch": "e",
            "generation": 1,
            "exported_at": "2026-01-01T00:00:00Z",
            "sample_count": 0,
            "samples": [],
            "checksum": "x",
        }
    ).encode(),
    "empty": b"",
}


def test_invalid_backups_are_rejected_without_touching_live_data(client, db_path):
    create(client, "KEEP-ME")

    for name, raw in INVALID_CASES.items():
        response = upload(client, None, raw=raw)
        assert response.status_code in (413, 422), (name, response.text)
        assert row_count(db_path) == 1, name

    assert client.get("/api/samples/KEEP-ME").status_code == 200


def test_checksum_tamper_is_rejected(client, db_path):
    create(client, "KEEP-ME")
    doc = backup_doc(client)
    tampered = json.loads(json.dumps(doc))
    tampered["samples"][0]["ph"] = 1.0
    response = upload(client, tampered)
    assert response.status_code == 422
    assert response.json()["warehouse_unchanged"] is True
    assert client.get("/api/samples/KEEP-ME").json()["sample"]["ph"] == 7.2


def test_semantically_invalid_sample_rejected_with_valid_checksum(client, db_path):
    create(client, "KEEP-ME")
    doc = backup_doc(client)
    broken = json.loads(json.dumps(doc))
    broken["samples"].append(
        {
            "sample_no": "BAD-PH",
            "station_no": "ST-1",
            "sampled_at": "2026-01-01T00:00:00Z",
            "ph": 99,
            "version": 1,
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    broken["sample_count"] = len(broken["samples"])
    broken["checksum"] = checksum_of(broken)
    response = upload(client, broken)
    assert response.status_code == 422
    assert client.get("/api/samples/BAD-PH").status_code == 404
    assert row_count(db_path) == 1


def test_duplicate_sample_numbers_rejected(client):
    doc = backup_doc(client)
    item = {
        "sample_no": "DUP",
        "station_no": "ST-1",
        "sampled_at": "2026-01-01T00:00:00Z",
        "ph": 7,
        "version": 1,
        "updated_at": "2026-01-01T00:00:00Z",
    }
    doc["samples"] = [item, dict(item)]
    doc["sample_count"] = 2
    doc["checksum"] = checksum_of(doc)
    response = upload(client, doc)
    assert response.status_code == 422
    assert "duplicate" in response.json()["message"]


# ------------------------------------------------------------------ restore


def test_restore_replaces_whole_set_and_bumps_generation(client, db_path):
    create(client, "OLD-ONLY")
    doc = backup_doc(client)

    # Warehouse moves on after the backup was taken.
    create(client, "NEW-ONLY", request_id="req-0000-0002")
    old_meta = meta(client)
    assert row_count(db_path) == 2

    response = upload(client, doc)
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "restored"
    assert body["sample_count"] == 1
    assert body["generation"] == old_meta["generation"] + 1
    assert body["epoch"] != old_meta["epoch"]

    # Complete old set, not a mixture.
    assert [s["sample_no"] for s in client.get("/api/samples").json()["samples"]] == [
        "OLD-ONLY"
    ]
    assert client.get("/api/samples/NEW-ONLY").status_code == 404
    new_meta = meta(client)
    assert new_meta["generation"] == old_meta["generation"] + 1
    assert new_meta["epoch"] == body["epoch"]
    assert new_meta["last_restore"]["sample_count"] == 1


def test_restore_preserves_sample_numbers_and_versions(client):
    create(client, "SAME-NO")
    client.put(
        "/api/samples/SAME-NO",
        json=payload(
            ph=6.5, base_version=1, request_id="req-0000-0002"
        ),
    )
    doc = backup_doc(client)
    assert doc["samples"][0]["version"] == 2

    response = upload(client, doc)
    assert response.status_code == 200
    restored = client.get("/api/samples/SAME-NO").json()["sample"]
    assert restored["version"] == 2
    assert restored["ph"] == 6.5


def test_reuploading_current_era_backup_still_starts_a_new_era(client):
    """Every restore is an era boundary, even when the file is up to date.

    This keeps the upload/restore interleaving unambiguous: a request is
    always either before or after one known switch point.
    """
    create(client, "SAME")
    doc = backup_doc(client)
    before = meta(client)
    response = upload(client, doc)
    assert response.status_code == 200
    assert response.json()["outcome"] == "restored"
    assert meta(client)["epoch"] != before["epoch"]
    assert meta(client)["generation"] == before["generation"] + 1
    assert client.get("/api/samples/SAME").status_code == 200


def test_restore_can_cross_site_only_via_matching_site(client, tmp_path):
    other_db = tmp_path / "other.db"
    with TestClient(create_app(str(other_db), site_id="other-site")) as other:
        create(other, "OTHER-SAMPLE")
        foreign = backup_doc(other)
    response = upload(client, foreign)
    assert response.status_code == 422
    assert "different site" in response.json()["message"]


# ------------------------------------------------------- epoch / idempotency


def test_write_with_stale_epoch_is_refused_after_restore(client):
    create(client, "OLD-ONLY")
    doc = backup_doc(client)
    stale_epoch = meta(client)["epoch"]
    upload(client, doc)
    new_epoch = meta(client)["epoch"]
    assert new_epoch != stale_epoch

    response = client.put(
        "/api/samples/OLD-ONLY",
        json=payload(
            ph=1.1,
            base_version=1,
            request_id="req-stale-0001",
            epoch=stale_epoch,
        ),
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "epoch_changed"
    assert body["epoch"] == new_epoch
    # Refused writes do not land.
    assert client.get("/api/samples/OLD-ONLY").json()["sample"]["ph"] == 7.2


def test_retry_of_pre_restore_request_does_not_replay_success(client):
    create(client, "SAME-NO")
    epoch_a = meta(client)["epoch"]
    write = payload(
        ph=6.0,
        base_version=1,
        request_id="req-replay-0001",
        epoch=epoch_a,
    )
    first = client.put("/api/samples/SAME-NO", json=write)
    assert first.status_code == 200
    doc = backup_doc(client)
    upload(client, doc)  # restore into a new era
    epoch_b = meta(client)["epoch"]
    assert epoch_b != epoch_a

    # Same request id and payload, same sample number and version as the
    # restored database: must NOT be reported as a successful replay.
    retry = client.put("/api/samples/SAME-NO", json=write)
    assert retry.status_code == 409
    assert retry.json()["error"] == "epoch_changed"
    assert retry.headers["X-Idempotent-Replay"] == "false"


def test_idempotency_records_cleared_on_restore(client, db_path):
    create(client, "SAME-NO")
    epoch_a = meta(client)["epoch"]
    client.put(
        "/api/samples/SAME-NO",
        json=payload(
            ph=6.0, base_version=1, request_id="req-clear-0001", epoch=epoch_a
        ),
    )
    doc = backup_doc(client)
    upload(client, doc)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM idempotency").fetchone()[0] == 0


def test_write_without_epoch_is_refused_after_restore(client):
    create(client, "SAME")
    doc = backup_doc(client)
    upload(client, doc)  # generation 2
    response = client.put(
        "/api/samples/SAME",
        json=payload(ph=1.5, base_version=1, request_id="req-noepoch-01"),
    )
    assert response.status_code == 409
    assert response.json()["error"] == "epoch_required"
    assert response.headers["X-Idempotent-Replay"] == "false"
    assert client.get("/api/samples/SAME").json()["sample"]["ph"] == 7.2


def test_legacy_database_without_meta_is_migrated(tmp_path):
    db_path = tmp_path / "legacy.db"
    # Simulate a database created by the previous release.
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE samples (
                sample_no TEXT PRIMARY KEY, station_no TEXT NOT NULL,
                sampled_at TEXT NOT NULL, ph REAL NOT NULL,
                version INTEGER NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE idempotency (
                request_id TEXT PRIMARY KEY, sample_no TEXT NOT NULL,
                payload_hash TEXT NOT NULL, response_status INTEGER NOT NULL,
                response_body TEXT NOT NULL, created_at TEXT NOT NULL
            );
            INSERT INTO samples VALUES ('LEGACY', 'ST-1',
                '2026-01-01T00:00:00Z', 7.0, 1, '2026-01-01T00:00:00Z');
            INSERT INTO idempotency VALUES ('legacy-req-1', 'LEGACY', 'hash', 200,
                '{"sample":{}}', '2026-01-01T00:00:00Z');
            """
        )
    with TestClient(create_app(str(db_path), site_id=SITE)) as client:
        found = client.get("/api/samples/LEGACY")
        assert found.status_code == 200
        info = meta(client)
        assert info["generation"] == 1
        assert info["epoch"]
        doc = backup_doc(client)
        assert doc["sample_count"] == 1
        restore = upload(client, doc)
        assert restore.status_code == 200


def test_concurrent_reader_sees_old_or_restored_set_never_partial(
    client, db_path, tmp_path
):
    for i in range(40):
        create(client, f"S{i:03d}", request_id=f"req-fill-{i:04d}")
    doc = backup_doc(client)

    # Remove most samples before restoring so a partial restore would be
    # visible as an intermediate count.
    doc["samples"] = doc["samples"][:5]
    doc["sample_count"] = 5
    doc["checksum"] = checksum_of(doc)

    observed: list[int] = []
    import threading

    stop = threading.Event()

    def reader():
        from app.store import snapshot_samples

        while not stop.is_set():
            observed.append(len(snapshot_samples(str(db_path))["samples"]))

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        response = upload(client, doc)
        assert response.status_code == 200
    finally:
        stop.set()
        thread.join()

    # Only the pre-restore size (40) or the restored size (5) may be observed.
    assert set(observed) <= {5, 40}, sorted(set(observed))


def test_failed_restore_write_rolls_back_completely(client, db_path, monkeypatch):
    """An error while writing the restored rows must leave the old db intact."""
    from app import db as db_module
    from app.backup import checksum_of
    from app.store import restore_backup

    create(client, "KEEP-A")
    create(client, "KEEP-B", request_id="req-0000-0002")
    before = client.get("/api/samples").json()
    before_meta = meta(client)

    doc = backup_doc(client)
    doc["samples"] = doc["samples"][:1]
    doc["sample_count"] = 1
    doc["checksum"] = checksum_of(doc)

    class Boom(RuntimeError):
        pass

    class ExplodingConnection:
        """Proxy that fails while inserting the restored rows."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def executemany(self, sql, rows):
            if "INSERT INTO samples" in sql:
                # Write one row, then fail, as a crash mid-transaction would.
                self._conn.executemany(sql, [list(rows)[0]])
                raise Boom("simulated crash mid-restore")
            return self._conn.executemany(sql, rows)

    import contextlib

    @contextlib.contextmanager
    def exploding_connect(database_path):
        with db_module.connect(database_path) as conn:
            yield ExplodingConnection(conn)

    monkeypatch.setattr("app.store.connect", exploding_connect)
    with pytest.raises(Boom):
        restore_backup(str(db_path), backup=doc, site_id=SITE)
    monkeypatch.undo()

    # Whole old set still present, era unchanged, no partial rows.
    after = client.get("/api/samples").json()
    assert after["samples"] == before["samples"]
    after_meta = meta(client)
    assert after_meta["epoch"] == before_meta["epoch"]
    assert after_meta["generation"] == before_meta["generation"]
    assert row_count(db_path) == 2


def test_restore_survives_reopen_after_uncommitted_journal(tmp_path):
    """A hot journal from an interrupted restore is rolled back on reopen."""
    import sqlite3

    db_path = tmp_path / "crash.db"
    with TestClient(create_app(str(db_path), site_id=SITE)) as client:
        create(client, "SURVIVOR")
        epoch = meta(client)["epoch"]

    # Simulate an interrupted restore: an explicit transaction with half the
    # work done, then the connection dropped without COMMIT.
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("DELETE FROM samples")
    conn.execute(
        "INSERT INTO samples VALUES ('HALF', 'ST-1', '2026-01-01T00:00:00Z', 7, 1, "
        "'2026-01-01T00:00:00Z')"
    )
    # Drop the connection (like a process exit) without committing.
    conn.close()

    with TestClient(create_app(str(db_path), site_id=SITE)) as client:
        samples = client.get("/api/samples").json()["samples"]
        assert [s["sample_no"] for s in samples] == ["SURVIVOR"]
        assert meta(client)["epoch"] == epoch


def test_concurrent_uploads_and_restore_have_unambiguous_ownership(client, db_path):
    """Interleaved writes either commit before the restore (and are replaced)
    or after it (and are refused); none may land ambiguously."""
    import threading

    from app.store import EpochMismatchError, restore_backup, upsert_sample

    create(client, "BASE")
    doc = backup_doc(client)
    old_epoch = meta(client)["epoch"]
    outcomes: list[str] = []
    errors: list[str] = []
    barrier = threading.Barrier(2)

    def writer(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            upsert_sample(
                str(db_path),
                sample_no=f"W-{index}",
                station_no="ST-W",
                sampled_at="2026-11-16T09:00:00Z",
                ph=7.0,
                base_version=0,
                request_id=f"concurrent-write-{index:04d}",
                client_epoch=old_epoch,
            )
            outcomes.append("committed-before")
        except EpochMismatchError:
            errors.append("refused-after")
        except Exception as exc:  # pragma: no cover - diagnostic
            errors.append(type(exc).__name__)

    thread = threading.Thread(target=writer, args=(1,))
    thread.start()
    barrier.wait(timeout=5)
    restore_backup(str(db_path), backup=doc, site_id=SITE)
    thread.join(timeout=15)

    # Exactly one side of the switch happened for the writer: either the write
    # committed before the restore (and was replaced), or it was refused after
    # it. Never a partial/ambiguous ownership.
    assert len(outcomes) + len(errors) == 1

    # The restored set is exactly the backup: a write that committed before
    # the restore is gone, and one refused after it never appeared.
    restored = client.get("/api/samples").json()
    assert [s["sample_no"] for s in restored["samples"]] == ["BASE"]
    assert restored["epoch"] != old_epoch


def test_backup_from_other_site_rejected(client):
    create(client, "MINE")
    doc = backup_doc(client)
    doc["site_id"] = "some-other-site"
    doc["checksum"] = checksum_of(doc)
    response = upload(client, doc)
    assert response.status_code == 422
    assert "different site" in response.json()["message"]
    assert client.get("/api/samples/MINE").status_code == 200


def test_restore_refuses_database_owned_by_another_site(client, db_path):
    from app.store import InvalidRestore, restore_backup

    create(client, "MINE")
    doc = backup_doc(client)
    # The file validates for this instance, but the live database says it
    # belongs to another site (e.g. wrong volume mounted).
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE meta SET value = 'misconfigured-site' WHERE key = 'site_id'"
        )
    with pytest.raises(InvalidRestore):
        restore_backup(str(db_path), backup=doc, site_id=SITE)
    # Nothing was replaced.
    assert row_count(db_path) == 1
