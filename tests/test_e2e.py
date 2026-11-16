"""End-to-end browser checks for offline sampling, retry and conflict flows.

Run with a live server:
    DATABASE_PATH=... uvicorn app.main:app --port 8077
    E2E_BASE_URL=http://127.0.0.1:8077 pytest tests/test_e2e.py
"""

from __future__ import annotations

import os
import time

import httpx
import pytest
from playwright.sync_api import Page, expect, sync_playwright

BASE = os.environ.get("E2E_BASE_URL", "http://127.0.0.1:8077")


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 960})
    page = context.new_page()
    page.goto(BASE, wait_until="networkidle")
    expect(page.locator("[data-testid='sw-status']")).to_have_text(
        "离线缓存就绪", timeout=15000
    )
    yield page
    context.close()


def api():
    return httpx.Client(base_url=BASE, timeout=5)


def test_server_reachable():
    assert httpx.get(f"{BASE}/api/health", timeout=5).status_code == 200


def latest(sample_no: str):
    response = api().get(f"/api/samples/{sample_no}")
    return response.status_code, response.json()


def fill_sample(page: Page, sample_no: str, station: str, utc: str, ph: str) -> None:
    page.fill("[data-testid='sample-no']", sample_no)
    page.fill("[data-testid='station-no']", station)
    page.fill("[data-testid='sampled-at']", utc)
    page.fill("[data-testid='ph']", ph)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待传")


def upload(page: Page) -> None:
    page.click("[data-testid='upload']")


def wait_status(page: Page, text: str, timeout: int = 15000) -> None:
    expect(page.locator("[data-testid='status-title']")).to_contain_text(
        text, timeout=timeout
    )


def wait_badge(page: Page, text: str, timeout: int = 20000) -> None:
    expect(page.locator("[data-testid='status-badge']")).to_have_text(
        text, timeout=timeout
    )


def wait_version(sample_no: str, version: int, timeout: float = 15) -> dict:
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        with api() as client:
            response = client.get(f"/api/samples/{sample_no}")
        if response.status_code == 200:
            last = response.json()["sample"]
            if last["version"] == version:
                return last
        time.sleep(0.15)
    raise AssertionError(f"warehouse never reached v{version}; last={last}")


def wait_saved(page: Page) -> None:
    expect(page.locator("[data-testid='saved-hint']")).to_contain_text(
        "本机已存", timeout=5000
    )
    # Allow the debounced IndexedDB write to reach disk.
    page.wait_for_timeout(300)


def test_offline_entry_reload_and_upload(page: Page):
    sample_no = f"OFF-{int(time.time() * 1000)}"

    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-testid='connection-text']")).to_have_text("离线")
    expect(page.locator("[data-testid='status-title']")).to_be_visible()

    fill_sample(page, sample_no, "ST-OFF-1", "2026-11-16T01:20", "6.75")
    wait_status(page, "待传")
    wait_saved(page)

    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-testid='connection-text']")).to_have_text("离线")
    expect(page.locator("[data-testid='sample-no']")).to_have_value(sample_no)
    expect(page.locator("[data-testid='station-no']")).to_have_value("ST-OFF-1")
    expect(page.locator("[data-testid='ph']")).to_have_value("6.75")
    expect(page.locator("[data-testid='sampled-at']")).to_have_value("2026-11-16T01:20")
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待传")
    expect(page.locator("[data-testid='sample-list']")).to_contain_text(sample_no)
    expect(page.locator("[data-testid='sample-list']")).to_contain_text("待传")

    page.context.set_offline(False)
    expect(page.locator("[data-testid='connection-text']")).to_have_text("在线")
    upload(page)
    wait_badge(page, "已入仓", timeout=20000)

    status, body = latest(sample_no)
    assert status == 200
    assert body["sample"]["station_no"] == "ST-OFF-1"
    assert body["sample"]["ph"] == 6.75
    assert body["sample"]["sampled_at"] == "2026-11-16T01:20:00Z"
    assert body["sample"]["version"] == 1


def test_lost_receipt_retry_after_reload_and_post_receipt_edits(page: Page):
    sample_no = f"LOST-{int(time.time() * 1000)}"
    fill_sample(page, sample_no, "ST-LOST", "2026-11-16T02:40", "7.10")
    wait_status(page, "待传")

    aborted = {"count": 0}

    def lose_receipt(route):
        # Let the request reach the warehouse, then drop the response.
        route.fetch()
        aborted["count"] += 1
        route.abort()

    page.route(f"**/api/samples/{sample_no}", lose_receipt)
    upload(page)
    expect(page.locator("[data-testid='status-title']")).to_contain_text(
        "尚未收到回执", timeout=15000
    )
    assert aborted["count"] == 1

    # The write actually landed even though the browser saw no receipt.
    status, body = latest(sample_no)
    assert status == 200
    assert body["sample"]["version"] == 1

    # Edits after sending must stay pending: the old receipt only covers what
    # was actually sent.
    page.fill("[data-testid='ph']", "7.45")
    wait_saved(page)
    page.unroute(f"**/api/samples/{sample_no}")

    page.reload(wait_until="networkidle")
    expect(page.locator("[data-testid='sample-no']")).to_have_value(sample_no)
    # init() retries the pending snapshot with the persisted request id. The
    # replay must not write again; the local 7.45 edit stays pending.
    expect(page.locator("[data-testid='status-title']")).to_contain_text(
        "已入仓 v1（上次提交）", timeout=20000
    )
    status, body = latest(sample_no)
    assert body["sample"]["version"] == 1  # no duplicate write
    assert body["sample"]["ph"] == 7.10

    # The newer local edit is still pending and uses the fresh version.
    wait_badge(page, "待传")
    expect(page.locator("[data-testid='status-note']")).to_contain_text("尚未上传")
    upload(page)
    wait_badge(page, "已入仓")
    stored = wait_version(sample_no, 2)
    assert stored["ph"] == 7.45


def test_conflict_keeps_both_copies_survives_reload(page: Page):
    sample_no = f"CONF-{int(time.time() * 1000)}"
    with api() as client:
        response = client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": "ST-A",
                "sampled_at": "2026-11-16T03:10:00Z",
                "ph": 7.0,
                "base_version": 0,
                "request_id": f"seed-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )
        assert response.status_code == 201

    page.click("[data-testid='refresh-warehouse']")
    expect(page.locator("[data-testid='warehouse-list']")).to_contain_text(sample_no)
    row = page.locator(".warehouse-item", has_text=sample_no)
    row.locator("button").click()
    expect(page.locator("[data-testid='sample-no']")).to_have_value(sample_no)
    expect(page.locator("[data-testid='station-no']")).to_have_value("ST-A")

    # Another worker corrects the warehouse copy.
    with api() as client:
        corrected = client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": "ST-B",
                "sampled_at": "2026-11-16T04:00:00Z",
                "ph": 8.4,
                "base_version": 1,
                "request_id": f"other-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )
        assert corrected.status_code == 200

    # Local edit based on v1.
    page.fill("[data-testid='station-no']", "ST-LOCAL")
    page.fill("[data-testid='ph']", "6.10")
    wait_saved(page)
    upload(page)

    expect(page.locator("[data-testid='conflict-panel']")).to_be_visible(timeout=15000)
    expect(page.locator("[data-field='local-station']")).to_have_text("ST-LOCAL")
    expect(page.locator("[data-field='local-ph']")).to_have_text("6.1")
    expect(page.locator("[data-field='server-station']")).to_have_text("ST-B")
    expect(page.locator("[data-field='server-ph']")).to_have_text("8.4")
    expect(page.locator("[data-field='server-version']")).to_have_text("v2")
    expect(page.locator("[data-testid='status-badge']")).to_have_text("冲突")

    with api() as client:
        untouched = client.get(f"/api/samples/{sample_no}").json()["sample"]
        assert untouched["station_no"] == "ST-B"
        assert untouched["version"] == 2

    # Conflict content survives an offline close/reopen.
    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-testid='conflict-panel']")).to_be_visible()
    expect(page.locator("[data-field='local-station']")).to_have_text("ST-LOCAL")
    expect(page.locator("[data-field='server-station']")).to_have_text("ST-B")
    expect(page.locator("[data-testid='tabs']")).to_contain_text("冲突")
    expect(page.locator("[data-count='conflict']")).to_have_text("1")

    page.context.set_offline(False)
    expect(page.locator("[data-testid='connection-text']")).to_have_text("在线")
    page.click("[data-testid='use-local']")
    wait_badge(page, "已入仓", timeout=20000)
    expect(page.locator("[data-testid='conflict-panel']")).to_be_hidden()

    with api() as client:
        resolved = client.get(f"/api/samples/{sample_no}").json()["sample"]
        assert resolved["station_no"] == "ST-LOCAL"
        assert resolved["ph"] == 6.1
        assert resolved["version"] == 3


def test_download_warehouse_sample_and_supplement(page: Page):
    sample_no = f"DL-{int(time.time() * 1000)}"
    with api() as client:
        response = client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": "ST-DL",
                "sampled_at": "2026-11-16T05:05:00Z",
                "ph": 7.8,
                "base_version": 0,
                "request_id": f"seed-dl-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )
        assert response.status_code == 201

    page.click("[data-testid='refresh-warehouse']")
    row = page.locator(".warehouse-item", has_text=sample_no)
    expect(row).to_be_visible()
    row.locator("button").click()
    expect(page.locator("[data-testid='sample-no']")).to_have_value(sample_no)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")

    page.fill("[data-testid='station-no']", "ST-DL-2")
    page.fill("[data-testid='ph']", "7.05")
    wait_saved(page)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待传")
    upload(page)
    # Pending titles can contain the words "已入仓 v1"; wait on the badge.
    wait_badge(page, "已入仓")
    updated = wait_version(sample_no, 2)
    assert updated["station_no"] == "ST-DL-2"
    assert updated["ph"] == 7.05


def test_unique_sample_number_reopens_same_record(page: Page):
    sample_no = f"UNIQ-{int(time.time() * 1000)}"
    fill_sample(page, sample_no, "ST-U1", "2026-11-16T06:15", "7.3")
    wait_saved(page)
    expect(page.locator("[data-count='all']")).to_have_text("1")

    page.click("[data-testid='new-sample']")
    page.fill("[data-testid='sample-no']", sample_no)
    page.locator("[data-testid='station-no']").click()
    expect(page.locator("[data-testid='sample-no']")).to_have_value(sample_no)
    expect(page.locator("[data-testid='station-no']")).to_have_value("ST-U1")
    expect(page.locator("[data-count='all']")).to_have_text("1")
    expect(page.locator("[data-testid='toast']")).to_contain_text("已在本机")


def test_receipt_only_confirms_sent_snapshot(page: Page):
    sample_no = f"EDIT-{int(time.time() * 1000)}"
    fill_sample(page, sample_no, "ST-E1", "2026-11-16T07:10", "7.00")
    wait_saved(page)

    # Queue the snapshot while offline, then keep editing before the receipt
    # can arrive. Reconnecting auto-retries the queued snapshot.
    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    upload(page)
    expect(page.locator("[data-testid='status-title']")).to_contain_text("已排队")
    page.fill("[data-testid='ph']", "7.90")
    wait_saved(page)

    page.context.set_offline(False)
    expect(page.locator("[data-testid='connection-text']")).to_have_text("在线")
    expect(page.locator("[data-testid='status-title']")).to_contain_text(
        "已入仓 v1", timeout=20000
    )
    # The receipt must not mark the newer edit as stored.
    wait_badge(page, "待传")
    expect(page.locator("[data-testid='sample-list']")).to_contain_text(
        "上次提交 v1 已入仓"
    )
    with api() as client:
        stored = client.get(f"/api/samples/{sample_no}").json()["sample"]
        assert stored["version"] == 1
        assert stored["ph"] == 7.0
        assert stored["sampled_at"] == "2026-11-16T07:10:00Z"

    upload(page)
    wait_badge(page, "已入仓")
    stored = wait_version(sample_no, 2)
    assert stored["ph"] == 7.9


def test_conflict_can_adopt_server_copy(page: Page):
    sample_no = f"ADOPT-{int(time.time() * 1000)}"
    with api() as client:
        client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": "ST-SEED",
                "sampled_at": "2026-11-16T08:00:00Z",
                "ph": 7.2,
                "base_version": 0,
                "request_id": f"seed-adopt-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )
        client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": "ST-OTHER",
                "sampled_at": "2026-11-16T08:30:00Z",
                "ph": 8.8,
                "base_version": 1,
                "request_id": f"other-adopt-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )

    page.fill("[data-testid='sample-no']", sample_no)
    page.fill("[data-testid='station-no']", "ST-MINE")
    page.fill("[data-testid='sampled-at']", "2026-11-16T09:00")
    page.fill("[data-testid='ph']", "5.5")
    wait_saved(page)
    upload(page)

    expect(page.locator("[data-testid='conflict-panel']")).to_be_visible(timeout=15000)
    page.click("[data-testid='use-server']")
    expect(page.locator("[data-testid='station-no']")).to_have_value("ST-OTHER")
    expect(page.locator("[data-testid='ph']")).to_have_value("8.8")
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")
    expect(page.locator("[data-testid='conflict-panel']")).to_be_hidden()

    with api() as client:
        stored = client.get(f"/api/samples/{sample_no}").json()["sample"]
        assert stored["version"] == 2
        assert stored["station_no"] == "ST-OTHER"


def test_download_does_not_clobber_unsent_local_edit(page: Page):
    sample_no = f"SAFE-{int(time.time() * 1000)}"
    with api() as client:
        client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": "ST-V1",
                "sampled_at": "2026-11-16T10:00:00Z",
                "ph": 7.0,
                "base_version": 0,
                "request_id": f"seed-safe-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )

    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=sample_no).locator("button").click()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")

    page.fill("[data-testid='station-no']", "ST-MY-EDIT")
    wait_saved(page)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待传")

    with api() as client:
        client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": "ST-V2",
                "sampled_at": "2026-11-16T10:30:00Z",
                "ph": 6.4,
                "base_version": 1,
                "request_id": f"other-safe-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )

    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=sample_no).locator("button").click()
    expect(page.locator("[data-testid='conflict-panel']")).to_be_visible()
    expect(page.locator("[data-field='local-station']")).to_have_text("ST-MY-EDIT")
    expect(page.locator("[data-field='server-station']")).to_have_text("ST-V2")
    expect(page.locator("[data-field='server-version']")).to_have_text("v2")

    with api() as client:
        stored = client.get(f"/api/samples/{sample_no}").json()["sample"]
        assert stored["station_no"] == "ST-V2"


def test_offline_queue_survives_reload_and_retries_same_snapshot(page: Page):
    sample_no = f"QUEUE-{int(time.time() * 1000)}"
    fill_sample(page, sample_no, "ST-Q", "2026-11-16T11:15", "7.65")
    wait_saved(page)

    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    upload(page)
    expect(page.locator("[data-testid='status-title']")).to_contain_text("已排队")
    page.wait_for_timeout(300)

    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-testid='connection-text']")).to_have_text("离线")
    expect(page.locator("[data-testid='status-title']")).to_contain_text("已排队")
    expect(page.locator("[data-testid='sample-list']")).to_contain_text("等待回执")

    page.context.set_offline(False)
    wait_badge(page, "已入仓")
    stored = wait_version(sample_no, 1)
    assert stored["ph"] == 7.65
    assert stored["station_no"] == "ST-Q"


def test_ph_range_is_enforced_in_form(page: Page):
    sample_no = f"RANGE-{int(time.time() * 1000)}"
    page.fill("[data-testid='sample-no']", sample_no)
    page.fill("[data-testid='station-no']", "ST-R")
    page.fill("[data-testid='sampled-at']", "2026-11-16T12:00")
    page.fill("[data-testid='ph']", "14.01")
    expect(page.locator("[data-testid='ph']")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("[data-testid='status-title']")).to_contain_text("还需填写pH")
    expect(page.locator("[data-testid='upload']")).to_be_disabled()

    page.fill("[data-testid='ph']", "13.99")
    expect(page.locator("[data-testid='ph']")).not_to_have_attribute(
        "aria-invalid", "true"
    )
    expect(page.locator("[data-testid='upload']")).to_be_enabled()

    page.fill("[data-testid='ph']", "14")
    expect(page.locator("[data-testid='upload']")).to_be_enabled()


def test_second_local_create_of_warehouse_sample_shows_conflict(page: Page):
    sample_no = f"CREATE-{int(time.time() * 1000)}"
    with api() as client:
        client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": "ST-CLOUD",
                "sampled_at": "2026-11-16T13:00:00Z",
                "ph": 7.7,
                "base_version": 0,
                "request_id": f"seed-create-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )

    # A different device records the same unique sample number offline.
    page.fill("[data-testid='sample-no']", sample_no)
    page.fill("[data-testid='station-no']", "ST-LOCAL")
    page.fill("[data-testid='sampled-at']", "2026-11-16T13:05")
    page.fill("[data-testid='ph']", "6.2")
    wait_saved(page)
    upload(page)

    expect(page.locator("[data-testid='conflict-panel']")).to_be_visible(timeout=15000)
    expect(page.locator("[data-field='local-station']")).to_have_text("ST-LOCAL")
    expect(page.locator("[data-field='server-station']")).to_have_text("ST-CLOUD")
    expect(page.locator("[data-field='server-version']")).to_have_text("v1")

    page.click("[data-testid='use-local']")
    wait_badge(page, "已入仓", timeout=20000)
    stored = wait_version(sample_no, 2)
    assert stored["station_no"] == "ST-LOCAL"
    assert stored["ph"] == 6.2


# --------------------------------------------------------------------------
# Backup / restore
# --------------------------------------------------------------------------


def seed(
    sample_no: str,
    station: str = "ST-SEED",
    ph: float = 7.0,
    utc: str = "2026-11-16T14:00:00Z",
):
    with api() as client:
        response = client.put(
            f"/api/samples/{sample_no}",
            json={
                "station_no": station,
                "sampled_at": utc,
                "ph": ph,
                "base_version": 0,
                "request_id": f"seed-{sample_no}",
                "epoch": warehouse_meta()["epoch"],
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["sample"]


def warehouse_meta():
    with api() as client:
        return client.get("/api/warehouse").json()


def backup_bytes() -> bytes:
    with api() as client:
        response = client.get("/api/backup")
        assert response.status_code == 200
        return response.content


def restore_via_api(raw: bytes | None = None):
    with api() as client:
        response = client.post(
            "/api/backup/restore",
            content=raw if raw is not None else backup_bytes(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 200, response.text
        return response.json()


def sample_row(sample_no: str):
    with api() as client:
        response = client.get(f"/api/samples/{sample_no}")
    sample = response.json().get("sample") if response.status_code == 200 else None
    return response.status_code, sample


def upload_backup_file(page: Page, path: str) -> None:
    page.set_input_files("[data-testid='backup-file']", path)
    expect(page.locator("[data-testid='restore-confirm']")).to_be_visible(timeout=8000)


def test_backup_download_and_restore_from_page(page: Page, tmp_path):
    sample_no = f"BK-{int(time.time() * 1000)}"
    seed(sample_no)
    before = warehouse_meta()

    with page.expect_download() as download_info:
        page.click("[data-testid='download-backup']")
    download = download_info.value
    path = tmp_path / "backup.json"
    download.save_as(str(path))
    doc = __import__("json").loads(path.read_text(encoding="utf-8"))
    assert doc["format"] == "river-sampling-backup"
    assert any(s["sample_no"] == sample_no for s in doc["samples"])

    upload_backup_file(page, str(path))
    expect(page.locator("[data-testid='restore-confirm-copy']")).to_contain_text("个样品")
    page.click("[data-testid='restore-confirm-yes']")

    expect(page.locator("[data-testid='backup-status']")).to_contain_text(
        f"第 {before['generation'] + 1} 代", timeout=20000
    )
    after = warehouse_meta()
    assert after["generation"] == before["generation"] + 1
    assert after["epoch"] != before["epoch"]
    status, row = sample_row(sample_no)
    assert status == 200
    assert row["station_no"] == "ST-SEED"


def test_invalid_backup_upload_leaves_warehouse_unchanged(page: Page, tmp_path):
    sample_no = f"BADBK-{int(time.time() * 1000)}"
    seed(sample_no)
    before = warehouse_meta()

    bad = tmp_path / "bad.json"
    bad.write_text(
        '{"format":"river-sampling-backup","samples":[]}\n', encoding="utf-8"
    )
    page.set_input_files("[data-testid='backup-file']", str(bad))
    expect(page.locator("[data-testid='backup-error']")).to_be_visible(timeout=8000)
    expect(page.locator("[data-testid='backup-error']")).to_contain_text("未改动现库")
    assert not page.locator("[data-testid='restore-confirm']").is_visible()

    after = warehouse_meta()
    assert after["epoch"] == before["epoch"]
    assert after["generation"] == before["generation"]
    status, row = sample_row(sample_no)
    assert status == 200
    assert row["station_no"] == "ST-SEED"


def test_restore_withdraws_stored_mark_and_rechecks(page: Page):
    stored_no = f"RST-{int(time.time() * 1000)}"
    draft_no = f"RSTD-{int(time.time() * 1000)}"
    seed(stored_no)

    # Snapshot the warehouse before this device edits the sample.
    import json

    from app.backup import checksum_of

    old_doc = json.loads(backup_bytes())

    # Put the stored sample in the warehouse from this device (so the local
    # record carries a real receipt), plus an unrelated never-uploaded draft.
    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=stored_no).locator("button").click()
    expect(page.locator("[data-testid='sample-no']")).to_have_value(stored_no)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")
    page.fill("[data-testid='ph']", "7.55")
    wait_saved(page)
    upload(page)
    wait_badge(page, "已入仓")

    page.click("[data-testid='new-sample']")
    page.fill("[data-testid='sample-no']", draft_no)
    page.fill("[data-testid='station-no']", "ST-DRAFT")
    page.fill("[data-testid='sampled-at']", "2026-11-16T15:10")
    page.fill("[data-testid='ph']", "6.66")
    wait_saved(page)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待传")

    # An administrator restores a backup taken earlier (same sample number and
    # version, different content; the era must still change).
    for item in old_doc["samples"]:
        if item["sample_no"] == stored_no:
            assert item["version"] == 1
    old_doc["checksum"] = checksum_of(old_doc)
    restore_via_api(json.dumps(old_doc).encode())

    # The page learns the new era by downloading the list.
    page.click("[data-testid='refresh-warehouse']")
    expect(page.locator("[data-testid='recheck-panel']")).to_be_visible(timeout=15000)
    expect(page.locator("[data-testid='recheck-list']")).to_contain_text(stored_no)

    # Old stored mark withdrawn; the local content is untouched.
    page.locator(".sample-card", has_text=stored_no).click()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待核对")
    expect(page.locator("[data-testid='upload']")).to_be_disabled()
    expect(page.locator("[data-testid='station-no']")).to_have_value("ST-SEED")
    expect(page.locator("[data-testid='ph']")).to_have_value("7.55")

    # The never-uploaded draft is kept and still pending; only records with an
    # old warehouse basis need review.
    expect(page.locator(".sample-card", has_text=draft_no)).to_contain_text("待传")
    expect(page.locator(".sample-card", has_text=draft_no)).not_to_contain_text(
        "待核对"
    )

    # Diff shows local vs restored values.
    item = page.locator(".recheck-item", has_text=stored_no)
    expect(item.locator("[data-testid='recheck-local']")).to_contain_text("7.55")
    expect(item.locator("[data-testid='recheck-server']")).to_contain_text("7")

    item.locator("[data-action='keep-local']").click()
    wait_badge(page, "已入仓", timeout=20000)
    status, row = sample_row(stored_no)
    assert status == 200
    assert row["station_no"] == "ST-SEED"
    assert row["ph"] == 7.55
    # The recheck upload derives from the restored v1, so exactly one bump.
    assert row["version"] == 2

    # The preserved draft can still be uploaded normally.
    page.locator(".sample-card", has_text=draft_no).click()
    upload(page)
    wait_badge(page, "已入仓")
    status, row = sample_row(draft_no)
    assert status == 200
    assert row["ph"] == 6.66


def test_retry_of_pre_restore_pending_request_is_not_reported_as_success(page: Page):
    sample_no = f"RTRY-{int(time.time() * 1000)}"
    seed(sample_no)
    old_meta = warehouse_meta()
    old_backup = backup_bytes()  # snapshot: sample at v1

    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=sample_no).locator("button").click()
    expect(page.locator("[data-testid='sample-no']")).to_have_value(sample_no)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")
    page.fill("[data-testid='ph']", "6.20")
    wait_saved(page)

    # The write reaches the warehouse but the receipt is lost.
    def lose_receipt(route):
        route.fetch()
        route.abort()

    page.route(f"**/api/samples/{sample_no}", lose_receipt)
    upload(page)
    expect(page.locator("[data-testid='status-title']")).to_contain_text(
        "尚未收到回执", timeout=15000
    )
    page.unroute(f"**/api/samples/{sample_no}")
    assert latest(sample_no)[1]["sample"]["version"] == 2

    # Restore the older snapshot: same sample number, version 1.
    restored = restore_via_api(old_backup)
    assert restored["epoch"] != old_meta["epoch"]

    # Retrying the old request must be refused, not replayed as success.
    page.click("[data-testid='refresh-warehouse']")
    expect(page.locator("[data-testid='recheck-panel']")).to_be_visible(timeout=15000)
    page.locator(".sample-card", has_text=sample_no).click()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待核对")
    expect(page.locator("[data-testid='status-title']")).to_contain_text("数仓已恢复")

    page.click("[data-testid='refresh-all']")
    page.wait_for_timeout(1000)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待核对")
    # The restored warehouse still holds v1, untouched by the stale retry.
    status, row = sample_row(sample_no)
    assert status == 200
    assert row["version"] == 1
    assert row["ph"] == 7.0

    # Review and re-upload deliberately.
    page.locator(".recheck-item", has_text=sample_no).locator(
        "[data-action='keep-local']"
    ).click()
    wait_badge(page, "已入仓", timeout=20000)
    status, row = sample_row(sample_no)
    assert row["version"] == 2
    assert row["ph"] == 6.2


def test_late_old_receipt_does_not_change_recheck_result(page: Page):
    sample_no = f"LATE-{int(time.time() * 1000)}"
    seed(sample_no)
    old_meta = warehouse_meta()
    old_backup = backup_bytes()

    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=sample_no).locator("button").click()
    expect(page.locator("[data-testid='sample-no']")).to_have_value(sample_no)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")
    page.fill("[data-testid='ph']", "5.50")
    wait_saved(page)

    held = {}

    def hold_request(route):
        # Keep the request un-sent so the test controls when it completes.
        held["route"] = route

    page.route(f"**/api/samples/{sample_no}", hold_request)
    upload(page)
    page.wait_for_function("() => true")
    deadline = time.time() + 10
    while "route" not in held and time.time() < deadline:
        page.wait_for_timeout(100)
    assert "route" in held

    restore_via_api(old_backup)
    page.click("[data-testid='refresh-warehouse']")
    expect(page.locator("[data-testid='recheck-panel']")).to_be_visible(timeout=15000)
    expect(page.locator(".sample-card", has_text=sample_no)).to_contain_text("待核对")

    # A pre-restore success receipt finally arrives, carrying the old era.
    held["route"].fulfill(
        status=200,
        headers={
            "Content-Type": "application/json",
            "X-Warehouse-Epoch": old_meta["epoch"],
            "X-Warehouse-Current-Epoch": old_meta["epoch"],
            "X-Idempotent-Replay": "false",
        },
        body=__import__("json").dumps(
            {
                "outcome": "updated",
                "sample": {
                    "sample_no": sample_no,
                    "station_no": "ST-SEED",
                    "sampled_at": "2026-11-16T14:00:00Z",
                    "ph": 5.5,
                    "version": 2,
                    "updated_at": "2026-11-16T16:00:00Z",
                },
            }
        ),
    )
    page.unroute(f"**/api/samples/{sample_no}")
    page.wait_for_timeout(800)

    # The stale receipt must not flip the record to stored, and the local
    # content kept for review must be unchanged.
    expect(page.locator(".sample-card", has_text=sample_no)).to_contain_text("待核对")
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待核对")
    item = page.locator(".recheck-item", has_text=sample_no)
    expect(item.locator("[data-testid='recheck-local']")).to_contain_text("5.5")
    status, row = sample_row(sample_no)
    assert row["version"] == 1
    assert row["ph"] == 7.0


def test_stale_open_page_cannot_write_on_old_basis_after_restore(page: Page):
    """A tab that never re-downloaded must not overwrite the restored data."""
    sample_no = f"STALE-{int(time.time() * 1000)}"
    seed(sample_no)
    old_backup = backup_bytes()  # v1, ph 7.0

    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=sample_no).locator("button").click()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")

    # Administrator restores the same sample number/version with older data.
    restore_via_api(old_backup)

    # The open page edits and uploads without re-downloading the list. Its
    # base is v1, which also exists in the restored database, but it belongs
    # to the previous era: the server must refuse it.
    page.fill("[data-testid='ph']", "9.90")
    wait_saved(page)
    upload(page)

    # The page is moved into the restore review instead of confirming a write.
    expect(page.locator("[data-testid='recheck-panel']")).to_be_visible(timeout=20000)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待核对")
    status, row = sample_row(sample_no)
    assert status == 200
    assert row["version"] == 1
    assert row["ph"] == 7.0

    # Nothing was written until the user explicitly chooses, then it applies
    # exactly once on top of the restored version.
    page.locator(".recheck-item", has_text=sample_no).locator(
        "[data-action='keep-local']"
    ).click()
    wait_badge(page, "已入仓", timeout=20000)
    status, row = sample_row(sample_no)
    assert row["version"] == 2
    assert row["ph"] == 9.9


def test_offline_draft_survives_restore_and_uploads_against_new_era(page: Page):
    """A draft created entirely offline has no old-era basis; after a restore
    it may still be uploaded, but only stamped with the restored era."""
    sample_no = f"OFFR-{int(time.time() * 1000)}"
    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    fill_sample(page, sample_no, "ST-OFF-R", "2026-11-16T17:20", "6.45")
    wait_saved(page)

    # A restore happens in the warehouse while this tab is offline.
    restore_via_api()
    new_epoch = warehouse_meta()["epoch"]

    page.context.set_offline(False)
    expect(page.locator("[data-testid='connection-text']")).to_have_text("在线")
    # Reconnect learns the new era and retries nothing stale.
    page.wait_for_timeout(800)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待传")

    upload(page)
    wait_badge(page, "已入仓", timeout=20000)
    status, row = sample_row(sample_no)
    assert status == 200
    assert row["version"] == 1
    assert row["ph"] == 6.45
    # The write belongs to the restored era, which is still current.
    assert warehouse_meta()["epoch"] == new_epoch
