from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(str(tmp_path / "test.db"))
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


def create(client, sample_no="RIVER-0001", **overrides):
    response = client.put(f"/api/samples/{sample_no}", json=payload(**overrides))
    assert response.status_code == 201, response.text
    return response.json()["sample"]


def test_create_and_fetch(client):
    sample = create(client)
    assert sample["version"] == 1
    assert sample["sampled_at"] == "2026-11-16T08:30:00Z"
    assert sample["ph"] == 7.2

    fetched = client.get("/api/samples/RIVER-0001").json()["sample"]
    assert fetched == sample


def test_update_bumps_version(client):
    create(client)
    response = client.put(
        "/api/samples/RIVER-0001",
        json=payload(
            station_no="ST-09",
            ph=6.85,
            base_version=1,
            request_id="req-0000-0002",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "updated"
    assert body["sample"]["version"] == 2
    assert body["sample"]["station_no"] == "ST-09"
    assert body["sample"]["ph"] == 6.85


def test_stale_version_returns_conflict_with_both_copies(client):
    create(client)
    client.put(
        "/api/samples/RIVER-0001",
        json=payload(
            station_no="ST-09", ph=6.5, base_version=1, request_id="req-0000-0002"
        ),
    )
    response = client.put(
        "/api/samples/RIVER-0001",
        json=payload(
            station_no="ST-01", ph=8.1, base_version=1, request_id="req-0000-0003"
        ),
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"] == "conflict"
    assert body["local"]["station_no"] == "ST-01"
    assert body["server"]["station_no"] == "ST-09"
    assert body["server"]["version"] == 2

    # Nothing was overwritten.
    stored = client.get("/api/samples/RIVER-0001").json()["sample"]
    assert stored["station_no"] == "ST-09"


def test_create_when_server_already_exists_conflicts(client):
    create(client)
    response = client.put(
        "/api/samples/RIVER-0001",
        json=payload(base_version=0, request_id="req-0000-0009"),
    )
    assert response.status_code == 409
    assert response.json()["server"]["version"] == 1


def test_lost_receipt_retry_is_idempotent(client):
    create(client)
    body = payload(base_version=1, ph=7.5, request_id="req-retry-0001")
    first = client.put("/api/samples/RIVER-0001", json=body)
    assert first.status_code == 200
    assert first.headers["X-Idempotent-Replay"] == "false"

    second = client.put("/api/samples/RIVER-0001", json=body)
    assert second.status_code == 200
    assert second.headers["X-Idempotent-Replay"] == "true"
    assert second.json() == first.json()

    # Exactly one version bump happened.
    stored = client.get("/api/samples/RIVER-0001").json()["sample"]
    assert stored["version"] == 2
    assert stored["ph"] == 7.5


def test_retry_after_success_does_not_report_conflict(client):
    create(client)
    body = payload(base_version=1, ph=6.0, request_id="req-retry-0002")
    assert client.put("/api/samples/RIVER-0001", json=body).status_code == 200
    # The client did not see the receipt and retries with stale base_version.
    retry = client.put("/api/samples/RIVER-0001", json=body)
    assert retry.status_code == 200
    assert retry.headers["X-Idempotent-Replay"] == "true"


def test_request_id_reuse_with_different_payload_rejected(client):
    create(client)
    body = payload(base_version=1, ph=7.5, request_id="req-reuse-0001")
    assert client.put("/api/samples/RIVER-0001", json=body).status_code == 200
    changed = dict(body, ph=8.0)
    response = client.put("/api/samples/RIVER-0001", json=changed)
    assert response.status_code == 422
    assert "request_id" in response.json()["error"]
    stored = client.get("/api/samples/RIVER-0001").json()["sample"]
    assert stored["ph"] == 7.5


def test_new_request_after_receipt_applies_new_change(client):
    create(client)
    first = payload(base_version=1, ph=7.5, request_id="req-seq-0001")
    assert client.put("/api/samples/RIVER-0001", json=first).status_code == 200
    second = payload(base_version=2, ph=8.2, request_id="req-seq-0002")
    response = client.put("/api/samples/RIVER-0001", json=second)
    assert response.status_code == 200
    assert response.json()["sample"]["version"] == 3
    assert response.json()["sample"]["ph"] == 8.2


def test_validation_bounds(client):
    assert (
        client.put(
            "/api/samples/RIVER-X", json=payload(ph=14.01, request_id="req-bound-0001")
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/samples/RIVER-X", json=payload(ph=-0.01, request_id="req-bound-0002")
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/samples/RIVER-X", json=payload(ph="7.2", request_id="req-bound-0003")
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/samples/RIVER-X", json=payload(ph=True, request_id="req-bound-0004")
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/samples/RIVER-X", json=payload(ph=0, request_id="req-bound-0005")
        ).status_code
        == 201
    )
    assert (
        client.put(
            "/api/samples/RIVER-Y", json=payload(ph=14, request_id="req-bound-0006")
        ).status_code
        == 201
    )

    assert (
        client.put(
            "/api/samples/RIVER-Z",
            json=payload(sampled_at="2026-11-16T08:30:00", request_id="req-bound-0007"),
        ).status_code
        == 422
    )
    assert (
        client.put(
            "/api/samples/RIVER-Z",
            json=payload(
                sampled_at="2026-11-16T08:30:00+08:00",
                request_id="req-bound-0008",
            ),
        ).status_code
        == 201
    )
    normalized = client.get("/api/samples/RIVER-Z").json()["sample"]
    assert normalized["sampled_at"] == "2026-11-16T00:30:00Z"


def test_sample_number_uniqueness_upsert_same_key(client):
    create(client, "RIVER-DUP")
    assert client.get("/api/samples").json()["samples"][0]["sample_no"] == "RIVER-DUP"
    # Same sample number can never create a second row.
    response = client.put(
        "/api/samples/RIVER-DUP",
        json=payload(base_version=0, request_id="req-dup-0001"),
    )
    assert response.status_code == 409


def test_service_worker_and_shell_served(client):
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
    assert response.headers["service-worker-allowed"] == "/"
    assert "no-cache" in response.headers["cache-control"]

    index = client.get("/")
    assert index.status_code == 200
    assert "河段水质采样" in index.text

    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "indexedDB" in script.text
