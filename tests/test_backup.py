"""Backup download / restore tests: atomicity, epochs and request fencing."""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import store
from app.main import create_app
from app.models import payload_hash


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture()
def client(db_path: Path):
    app = create_app(str(db_path))
    with TestClient(app) as test_client:
        yield test_client


def payload(**overrides):
    base = {
        "station_no": "ST-04",
        "sampled_at": "2026-11-16T08:30:00Z",
        "ph": 7.2,
        "base_version": 0,
        "request_id": "req-0000-0001",
        "base_epoch": 1,
    }
    base.update(overrides)
    return base


def create(client, sample_no="RIVER-0001", **overrides):
    response = client.put(f"/api/samples/{sample_no}", json=payload(**overrides))
    assert response.status_code in (200, 201), response.text
    return response.json()


def reseal(document):
    """Recompute the checksum after changing content, to exercise deeper checks."""
    body = {key: value for key, value in document.items() if key != "checksum"}
    document["checksum"] = payload_hash(body)
    return document


def download(client):
    response = client.get("/api/backup")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert "attachment" in response.headers["content-disposition"]
    return json.loads(response.content)


def restore(client, document):
    return client.post(
        "/api/restore",
        content=json.dumps(document, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


def test_backup_contains_samples_and_identity(client):
    created = create(client)["sample"]
    document = download(client)
    assert document["format"] == "river-sampling-backup"
    assert document["format_version"] == 1
    assert document["site_id"] == "river-site"
    assert document["sample_count"] == 1
    assert document["samples"][0]["sample_no"] == created["sample_no"]
    assert document["samples"][0]["version"] == created["version"]
    assert document["epoch"] == 1


def test_restore_replaces_warehouse_and_bumps_epoch(client):
    create(client, "RIVER-OLD")
    document = download(client)

    # Warehouse moves on after the backup was taken.
    created = create(
        client,
        "RIVER-OLD",
        station_no="ST-09",
        base_version=1,
        request_id="req-after-backup",
    )
    assert created["outcome"] == "updated"
    create(client, "RIVER-EXTRA", request_id="req-extra")

    response = restore(client, document)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["restored"]["sample_count"] == 1
    assert body["warehouse"]["epoch"] == 2
    assert body["warehouse"]["restore_count"] == 1

    listed = client.get("/api/samples").json()
    assert [row["sample_no"] for row in listed["samples"]] == ["RIVER-OLD"]
    assert listed["samples"][0]["station_no"] == "ST-04"
    assert listed["samples"][0]["version"] == 1
    assert listed["warehouse"]["epoch"] == 2


def test_restore_is_idempotent_when_replaying_same_backup(client):
    create(client)
    document = download(client)
    first = restore(client, document)
    assert first.status_code == 200

    # A backup downloaded from the pre-restore warehouse still restores the
    # same content, but each restore is a new epoch.
    second = restore(client, document)
    assert second.status_code == 200
    assert second.json()["warehouse"]["epoch"] == 3
    assert client.get("/api/samples").json()["warehouse"]["epoch"] == 3


@pytest.mark.parametrize(
    "mutate, message_fragment, resealed",
    [
        (lambda doc: doc.update(site_id="other-site"), "site", False),
        (lambda doc: doc.update(format_version=99), "version", False),
        (lambda doc: doc.update(format="something-else"), "format", False),
        (lambda doc: doc.update(sample_count=9), "sample_count", True),
        (
            lambda doc: doc["samples"][0].update(sample_no="bad sample!"),
            "sample_no",
            True,
        ),
        (lambda doc: doc["samples"][0].update(ph=15), "ph", True),
        (lambda doc: doc["samples"][0].update(version=0), "version", True),
        (
            lambda doc: doc["samples"][0].update(updated_at="yesterday"),
            "updated_at",
            True,
        ),
        (lambda doc: doc["samples"][0].update(unknown_field=1), "sample", True),
        (
            lambda doc: (
                doc["samples"].append(dict(doc["samples"][0])),
                doc.update(sample_count=doc["sample_count"] + 1),
            ),
            "duplicate",
            True,
        ),
        (lambda doc: doc.update(checksum="0" * 64), "checksum", False),
        (lambda doc: doc.pop("checksum"), "exactly", False),
        (lambda doc: doc.update(extra=True), "exactly", False),
    ],
)
def test_invalid_backup_never_touches_live_warehouse(
    client, mutate, message_fragment, resealed
):
    create(client, "RIVER-KEEP")
    document = download(client)
    mutate(document)
    if resealed:
        reseal(document)
    response = restore(client, document)
    assert response.status_code == 422, response.text
    assert message_fragment in response.json()["message"]

    listed = client.get("/api/samples").json()
    assert [row["sample_no"] for row in listed["samples"]] == ["RIVER-KEEP"]
    assert listed["warehouse"]["epoch"] == 1
    assert listed["warehouse"]["restore_count"] == 0


@pytest.mark.parametrize(
    "raw",
    [b"", b"not json", b"[]", b'{"format": "river-sampling-backup"}', b"\xff\xfe"],
)
def test_malformed_upload_rejected(client, raw):
    create(client, "RIVER-KEEP")
    response = client.post(
        "/api/restore", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert client.get("/api/samples").json()["warehouse"]["epoch"] == 1


def test_upload_from_other_restore_is_fenced_by_epoch(client):
    create(client, "RIVER-A")
    old = download(client)
    # Snapshot the request a browser had prepared against epoch 1.
    stale = payload(
        station_no="ST-STALE",
        base_version=1,
        request_id="req-stale-0001",
        base_epoch=1,
    )
    assert restore(client, old).status_code == 200

    response = client.put("/api/samples/RIVER-A", json=stale)
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "restore_conflict"
    assert body["base_epoch"] == 1
    assert body["warehouse"]["epoch"] == 2
    # Nothing was written from the old basis.
    stored = client.get("/api/samples/RIVER-A").json()["sample"]
    assert stored["version"] == 1
    assert stored["station_no"] == "ST-04"


def test_same_sample_and_version_after_restore_cannot_be_written_by_old_page(client):
    create(client, "RIVER-SAME")
    document = download(client)
    assert restore(client, document).status_code == 200

    # Restored warehouse has the same sample number AND version 1, exactly what
    # the pre-restore page believed it was editing.
    response = client.put(
        "/api/samples/RIVER-SAME",
        json=payload(
            station_no="ST-FROM-OLD-PAGE",
            base_version=1,
            request_id="req-old-page-01",
            base_epoch=1,
        ),
    )
    assert response.status_code == 409
    assert response.json()["error"] == "restore_conflict"
    stored = client.get("/api/samples/RIVER-SAME").json()["sample"]
    assert stored["station_no"] == "ST-04"
    assert stored["version"] == 1

    # The same edit succeeds once it names the live epoch.
    accepted = client.put(
        "/api/samples/RIVER-SAME",
        json=payload(
            station_no="ST-FROM-OLD-PAGE",
            base_version=1,
            request_id="req-new-page-01",
            base_epoch=2,
        ),
    )
    assert accepted.status_code == 200
    assert accepted.json()["sample"]["version"] == 2


def test_old_request_retry_after_restore_is_not_reported_as_success(client):
    created = create(client, "RIVER-RETRY")
    assert created["sample"]["version"] == 1
    document = download(client)

    # This write landed before the restore but its receipt was lost.
    in_flight = payload(
        station_no="ST-IN-FLIGHT",
        base_version=1,
        request_id="req-lost-receipt",
        base_epoch=1,
    )
    first = client.put("/api/samples/RIVER-RETRY", json=in_flight)
    assert first.status_code == 200
    assert restore(client, document).status_code == 200

    retry = client.put("/api/samples/RIVER-RETRY", json=in_flight)
    assert retry.status_code == 409
    assert retry.json()["error"] == "restore_conflict"
    # It must not claim the restored warehouse holds the write.
    stored = client.get("/api/samples/RIVER-RETRY").json()["sample"]
    assert stored["station_no"] == "ST-04"
    assert stored["version"] == 1


def test_restore_drops_pre_restore_idempotency_receipts(client, db_path):
    create(client)
    document = download(client)
    restore(client, document)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT epoch, request_id FROM idempotency ORDER BY epoch"
        ).fetchall()
    assert rows == []


def test_generation_mismatch_refused(client):
    create(client)
    live = client.get("/api/warehouse").json()["warehouse"]
    response = client.put(
        "/api/samples/RIVER-NEW",
        json=payload(
            request_id="req-gen-0001",
            base_epoch=live["epoch"],
            generation="not-the-live-generation",
        ),
    )
    assert response.status_code == 409
    assert response.json()["error"] == "restore_conflict"

    good = client.put(
        "/api/samples/RIVER-NEW",
        json=payload(
            request_id="req-gen-0002",
            base_epoch=live["epoch"],
            generation=live["generation"],
        ),
    )
    assert good.status_code == 201


def test_preexisting_records_without_epoch_still_upload_until_first_restore(client):
    # A build that predates epochs sends base_epoch 0.
    response = client.put(
        "/api/samples/RIVER-LEGACY",
        json=payload(request_id="req-legacy-01", base_epoch=0),
    )
    assert response.status_code == 201

    document = download(client)
    restore(client, document)
    rejected = client.put(
        "/api/samples/RIVER-LEGACY",
        json=payload(
            station_no="ST-LATER",
            base_version=1,
            request_id="req-legacy-02",
            base_epoch=0,
        ),
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"] == "restore_conflict"


def _seed_v1_schema(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE samples (
                sample_no TEXT PRIMARY KEY,
                station_no TEXT NOT NULL,
                sampled_at TEXT NOT NULL,
                ph REAL NOT NULL,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE idempotency (
                request_id TEXT PRIMARY KEY,
                sample_no TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                response_status INTEGER NOT NULL,
                response_body TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO samples VALUES (
              'RIVER-OLD', 'ST-01', '2026-01-01T00:00:00Z', 7.0, 1,
              '2026-01-01T00:00:00Z'
            );
            """
        )
    finally:
        conn.close()


def test_database_upgrade_keeps_legacy_idempotency_replay(tmp_path: Path):
    """A request queued by the pre-epoch build must still replay after upgrade."""
    path = tmp_path / "legacy-replay.db"
    _seed_v1_schema(path)
    body = {
        "station_no": "ST-01",
        "sampled_at": "2026-01-01T00:00:00Z",
        "ph": 7.0,
        "base_version": 1,
    }
    fingerprint = payload_hash({"sample_no": "RIVER-OLD", **body})
    response_body = json.dumps(
        {"outcome": "updated", "sample": {"sample_no": "RIVER-OLD", "version": 2}}
    )
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "INSERT INTO idempotency (request_id, sample_no, payload_hash, "
            "response_status, response_body, created_at) VALUES (?,?,?,?,?,?)",
            (
                "req-legacy-replay",
                "RIVER-OLD",
                fingerprint,
                200,
                response_body,
                "2026-01-01T00:00:00Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    app = create_app(str(path))
    with TestClient(app) as client:
        replay = client.put(
            "/api/samples/RIVER-OLD",
            json={
                **body,
                "request_id": "req-legacy-replay",
                "base_epoch": 0,
            },
        )
        assert replay.status_code == 200, replay.text
        assert replay.headers["X-Idempotent-Replay"] == "true"
        stored = client.get("/api/samples/RIVER-OLD").json()["sample"]
        assert stored["version"] == 1  # the stored receipt did not write again


def test_database_upgrade_keeps_existing_samples(tmp_path: Path):
    path = tmp_path / "legacy.db"
    _seed_v1_schema(path)
    app = create_app(str(path))
    with TestClient(app) as client:
        listed = client.get("/api/samples").json()
        assert [row["sample_no"] for row in listed["samples"]] == ["RIVER-OLD"]
        assert listed["warehouse"]["epoch"] == 1
        backup = client.get("/api/backup")
        assert backup.status_code == 200
        assert backup.json()["sample_count"] == 1
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(idempotency)")}
    assert "epoch" in columns


def test_failed_restore_in_transaction_rolls_back(client, db_path, monkeypatch):
    create(client, "RIVER-KEEP")
    document = download(client)

    class Boom(Exception):
        pass

    original = store._set_meta

    def explode(conn, key, value):
        if key == "generation":
            raise Boom("process interrupted")
        return original(conn, key, value)

    monkeypatch.setattr(store, "_set_meta", explode)
    with pytest.raises(Boom):
        restore(client, document)

    listed = client.get("/api/samples").json()
    assert [row["sample_no"] for row in listed["samples"]] == ["RIVER-KEEP"]
    assert listed["warehouse"]["epoch"] == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0] == 1


def test_concurrent_restore_and_upload_each_belongs_to_one_side(client, db_path):
    create(client, "RIVER-CONCURRENT")
    document = download(client)
    results = {}

    def do_restore():
        app = create_app(str(db_path))
        with TestClient(app) as other:
            results["restore"] = restore(other, document).status_code

    def do_upload():
        app = create_app(str(db_path))
        with TestClient(app) as other:
            response = other.put(
                "/api/samples/RIVER-CONCURRENT",
                json=payload(
                    station_no="ST-CONCURRENT",
                    base_version=1,
                    request_id="req-concurrent-1",
                    base_epoch=1,
                ),
            )
            results["upload"] = response.status_code

    threads = [threading.Thread(target=do_restore), threading.Thread(target=do_upload)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert results["restore"] == 200
    assert results["upload"] in (200, 409)
    final = client.get("/api/samples/RIVER-CONCURRENT").json()["sample"]
    if results["upload"] == 200:
        # The upload serialised before the restore: the backup replaced it.
        assert final["station_no"] == "ST-04"
        assert final["version"] == 1
    else:
        # The upload lost the race and was refused; the restore still won.
        assert final["station_no"] == "ST-04"
    # Either way, no half-restored warehouse and a single epoch bump.
    listing = client.get("/api/samples").json()
    assert listing["warehouse"]["epoch"] == 2
    assert listing["warehouse"]["restore_count"] == 1


def test_warehouse_read_is_one_snapshot(client):
    create(client, "RIVER-SNAP")
    response = client.get("/api/warehouse")
    body = response.json()
    assert body["warehouse"]["epoch"] == 1
    assert [row["sample_no"] for row in body["samples"]] == ["RIVER-SNAP"]


def test_reads_during_restore_see_old_or_new_never_partial(client, db_path):
    for index in range(20):
        create(
            client,
            f"RIVER-READ-{index:02d}",
            request_id=f"req-read-{index:02d}",
        )
    document = download(client)
    # The restored copy deliberately has a different number of samples, so a
    # torn read would be visible as a count that is neither 20 nor 3.
    document["samples"] = document["samples"][:3]
    document["sample_count"] = 3
    body = {key: value for key, value in document.items() if key != "checksum"}
    document["checksum"] = payload_hash(body)

    observed: list[int] = []
    stop = threading.Event()

    def reader():
        while not stop.is_set():
            listing = client.get("/api/samples").json()
            observed.append(len(listing["samples"]))
            # Samples and warehouse metadata must come from the same snapshot.
            epoch = listing["warehouse"]["epoch"]
            if epoch == 1:
                assert len(listing["samples"]) == 20
            else:
                # 3 from the backup, or 4 after the following upload; never a
                # partially restored 20-sample/3-sample mixture.
                assert len(listing["samples"]) in (3, 4)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for _ in range(5):
            restore(client, document)
            create(
                client,
                "RIVER-READ-NEW",
                base_epoch=client.get("/api/warehouse").json()["warehouse"]["epoch"],
                request_id=f"req-read-new-{time.time()}",
            )
    finally:
        stop.set()
        thread.join()

    assert observed
    assert set(observed) <= {20, 3, 4}


def test_interrupted_restore_transaction_leaves_no_half_warehouse(client, db_path):
    """Simulates the process dying before COMMIT: SQLite discards the whole swap."""
    create(client, "RIVER-OLD-A")
    create(client, "RIVER-OLD-B", request_id="req-old-b")

    crashed = sqlite3.connect(db_path, isolation_level=None)
    crashed.execute("BEGIN IMMEDIATE")
    crashed.execute("DELETE FROM samples")
    crashed.execute(
        "INSERT INTO samples VALUES "
        "('RIVER-HALF', 'ST-H', '2026-01-01T00:00:00Z', 7.0, 1, '2026-01-01T00:00:00Z')"
    )
    crashed.execute("UPDATE meta SET value = '99' WHERE key = 'epoch'")
    # No COMMIT: the connection is abandoned, exactly like a killed process.
    crashed.close()

    listing = client.get("/api/samples").json()
    assert [row["sample_no"] for row in listing["samples"]] == [
        "RIVER-OLD-A",
        "RIVER-OLD-B",
    ]
    assert listing["warehouse"]["epoch"] == 1
