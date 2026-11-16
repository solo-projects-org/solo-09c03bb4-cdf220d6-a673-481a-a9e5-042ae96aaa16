"""End-to-end browser checks for offline sampling, retry and conflict flows.

Run with a live server:
    DATABASE_PATH=... uvicorn app.main:app --port 8077
    E2E_BASE_URL=http://127.0.0.1:8077 pytest tests/test_e2e.py
"""

from __future__ import annotations

import json
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
                **epoch_fields(),
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
                **epoch_fields(),
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
                **epoch_fields(),
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
                **epoch_fields(),
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
                **epoch_fields(),
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
                **epoch_fields(),
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
                **epoch_fields(),
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
                **epoch_fields(),
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
# Backup, restore and local reconciliation
# --------------------------------------------------------------------------


def server_epoch(sample_no: str | None = None):
    response = httpx.get(f"{BASE}/api/warehouse", timeout=5)
    response.raise_for_status()
    return response.json()["warehouse"]


def restore_from(document: dict):
    return httpx.post(f"{BASE}/api/restore", json=document, timeout=10)


def backup_document() -> dict:
    response = httpx.get(f"{BASE}/api/backup", timeout=10)
    response.raise_for_status()
    return response.json()


def upload_backup(page: Page, document: dict, tmp_path) -> None:
    path = tmp_path / "upload.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    page.set_input_files("[data-testid='restore-file']", str(path))
    expect(page.locator("[data-testid='restore-upload']")).to_be_enabled()
    page.click("[data-testid='restore-upload']")
    expect(page.locator("[data-testid='restore-result']")).to_contain_text(
        "已恢复", timeout=15000
    )


def epoch_fields() -> dict:
    warehouse = server_epoch()
    return {"base_epoch": warehouse["epoch"], "generation": warehouse["generation"]}


def seed(sample_no: str, station: str, utc: str, ph: float, base_version: int = 0):
    request_id = f"seed-{sample_no}-{base_version}-{time.time()}"
    warehouse = server_epoch()
    response = httpx.put(
        f"{BASE}/api/samples/{sample_no}",
        json={
            "station_no": station,
            "sampled_at": utc,
            "ph": ph,
            "base_version": base_version,
            "request_id": request_id,
            "base_epoch": warehouse["epoch"],
            "generation": warehouse["generation"],
        },
        timeout=10,
    )
    assert response.status_code in (200, 201), response.text
    return response.json()["sample"]


def test_backup_page_downloads_file(page: Page, tmp_path):
    sample_no = f"BAK-{int(time.time() * 1000)}"
    seed(sample_no, "ST-BAK", "2026-11-16T14:00:00Z", 7.1)

    with page.expect_download() as download_info:
        page.click("[data-testid='download-backup']")
    download = download_info.value
    target = tmp_path / download.suggested_filename
    download.save_as(target)
    document = json.loads(target.read_text("utf-8"))
    assert document["format"] == "river-sampling-backup"
    assert any(row["sample_no"] == sample_no for row in document["samples"])
    expect(page.locator("[data-testid='restore-result']")).to_contain_text(
        "已下载本站备份"
    )


def test_backup_page_upload_restores_and_flags_local_review(page: Page, tmp_path):
    sample_no = f"UPL-{int(time.time() * 1000)}"
    seed(sample_no, "ST-ORIGINAL", "2026-11-16T15:00:00Z", 7.2)
    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=sample_no).locator("button").click()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")

    document = backup_document()
    # The backup is taken now; then the warehouse diverges (bad admin edit).
    seed(sample_no, "ST-WRONG", "2026-11-16T15:00:00Z", 9.9, base_version=1)
    wait_version(sample_no, 2)

    path = tmp_path / "site-backup.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    page.set_input_files("[data-testid='restore-file']", str(path))
    expect(page.locator("[data-testid='restore-upload']")).to_be_enabled()
    page.click("[data-testid='restore-upload']")
    expect(page.locator("[data-testid='restore-result']")).to_contain_text(
        "已恢复", timeout=15000
    )

    # Warehouse is back to the backup content.
    with api() as client:
        restored = client.get(f"/api/samples/{sample_no}").json()["sample"]
    assert restored["station_no"] == "ST-ORIGINAL"
    assert restored["version"] == 1
    assert server_epoch()["restore_count"] >= 1

    # The local page withdrew its old "已入仓" marker and asks for review.
    expect(page.locator("[data-testid='rebase-panel']")).to_be_visible()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("需核对")
    expect(page.locator("[data-testid='restore-banner']")).to_be_visible()
    expect(page.locator("[data-field='rebase-server-station']")).to_have_text(
        "ST-ORIGINAL"
    )
    expect(page.locator("[data-field='rebase-local-state']")).to_contain_text("已撤销")
    expect(page.locator("[data-count='rebase']")).to_have_text("1")

    # Adopting the restored copy stores no stale receipt and leaves stored state.
    page.click("[data-testid='rebase-take-server']")
    expect(page.locator("[data-testid='rebase-panel']")).to_be_hidden()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")
    expect(page.locator("[data-testid='station-no']")).to_have_value("ST-ORIGINAL")


def test_invalid_backup_upload_leaves_live_warehouse_untouched(page: Page, tmp_path):
    sample_no = f"BAD-{int(time.time() * 1000)}"
    seed(sample_no, "ST-INTACT", "2026-11-16T16:00:00Z", 7.4)
    before = httpx.get(f"{BASE}/api/samples", timeout=5).json()

    broken = backup_document()
    broken["site_id"] = "some-other-site"
    path = tmp_path / "wrong-site.json"
    path.write_text(json.dumps(broken), encoding="utf-8")
    page.set_input_files("[data-testid='restore-file']", str(path))
    page.click("[data-testid='restore-upload']")
    expect(page.locator("[data-testid='restore-result']")).to_contain_text(
        "现库未被改动", timeout=15000
    )
    expect(page.locator("[data-testid='restore-result']")).to_have_attribute(
        "data-tone", "error"
    )

    after = httpx.get(f"{BASE}/api/samples", timeout=5).json()
    assert after == before
    expect(page.locator("[data-testid='rebase-panel']")).to_be_hidden()


def test_offline_draft_survives_restore_and_is_reviewed(page: Page):
    sample_no = f"DRAFT-{int(time.time() * 1000)}"
    # A local-only draft that has never been uploaded.
    fill_sample(page, sample_no, "ST-DRAFT", "2026-11-16T17:00:00Z"[:16], "7.6")
    wait_saved(page)

    document = backup_document()
    assert restore_from(document).status_code == 200
    new_epoch = server_epoch()["epoch"]

    page.click("[data-testid='refresh-warehouse']")
    # A never-sent draft has no warehouse basis: it stays editable as a draft.
    expect(page.locator("[data-testid='status-badge']")).to_have_text("待传")
    expect(page.locator("[data-testid='station-no']")).to_have_value("ST-DRAFT")
    expect(page.locator("[data-testid='rebase-panel']")).to_be_hidden()

    # Uploading it after the restore succeeds against the new epoch.
    upload(page)
    wait_badge(page, "已入仓")
    stored = wait_version(sample_no, 1)
    assert stored["station_no"] == "ST-DRAFT"
    assert server_epoch()["epoch"] == new_epoch


def test_pre_restore_page_cannot_write_after_restore_even_with_same_key(page: Page):
    sample_no = f"FENCE-{int(time.time() * 1000)}"
    seed(sample_no, "ST-BEFORE", "2026-11-16T18:00:00Z", 7.0)
    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=sample_no).locator("button").click()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")

    # The page goes offline with unsent edits based on v1.
    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    page.fill("[data-testid='station-no']", "ST-OLD-PAGE")
    wait_saved(page)
    upload(page)
    expect(page.locator("[data-testid='status-title']")).to_contain_text("已排队")

    # A restore reuses the same sample number and version.
    document = backup_document()
    assert restore_from(document).status_code == 200

    page.context.set_offline(False)
    expect(page.locator("[data-testid='connection-text']")).to_have_text("在线")

    # The old request is fenced: it must not be applied just because the
    # restored warehouse has version 1 for the same sample number.
    expect(page.locator("[data-testid='rebase-panel']")).to_be_visible(timeout=20000)
    expect(page.locator("[data-testid='status-badge']")).to_have_text("需核对")
    with api() as client:
        stored = client.get(f"/api/samples/{sample_no}").json()["sample"]
    assert stored["station_no"] == "ST-BEFORE"
    assert stored["version"] == 1

    # Draft and unconfirmed snapshot are both preserved in the review.
    expect(page.locator("[data-field='rebase-local-state']")).to_contain_text(
        "未确认提交"
    )
    expect(page.locator("[data-testid='station-no']")).to_have_value("ST-OLD-PAGE")

    page.click("[data-testid='rebase-keep-local']")
    wait_badge(page, "已入仓", timeout=20000)
    stored = wait_version(sample_no, 2)
    assert stored["station_no"] == "ST-OLD-PAGE"


def test_late_old_receipt_does_not_change_review_or_show_stored(page: Page):
    sample_no = f"LATE-{int(time.time() * 1000)}"
    fill_sample(page, sample_no, "ST-LATE", "2026-11-16T19:00", "7.15")
    wait_saved(page)

    # Hold the upload response so the write lands but the receipt arrives late.
    held = {}

    def hold_response(route):
        response = route.fetch()
        held["response"] = response
        held["route"] = route

    page.route(f"**/api/samples/{sample_no}", hold_response)
    upload(page)
    # Wait until the write reached the warehouse but its response is held.
    deadline = time.time() + 15
    while time.time() < deadline and "response" not in held:
        page.wait_for_timeout(100)
    assert "response" in held
    assert held["response"].status == 201

    document = backup_document()
    assert restore_from(document).status_code == 200

    # Reconnect: the client sees the restore and opens a review.
    page.click("[data-testid='refresh-warehouse']")
    expect(page.locator("[data-testid='rebase-panel']")).to_be_visible()
    page.click("[data-testid='rebase-keep-unresolved']")
    expect(page.locator("[data-testid='status-badge']")).to_have_text("需核对")

    # Deliver the pre-restore receipt now (the request was held open).
    response = held["response"]
    held["route"].fulfill(
        status=response.status,
        headers={k: v for k, v in response.headers.items()},
        body=response.body(),
    )
    page.wait_for_timeout(500)

    expect(page.locator("[data-testid='status-badge']")).to_have_text("需核对")
    expect(page.locator("[data-testid='rebase-panel']")).to_be_visible()
    expect(page.locator("[data-testid='rebase-note']")).to_contain_text(
        "迟到回执", timeout=5000
    )
    # The restored warehouse does not have the pre-restore upload.
    with api() as client:
        response = client.get(f"/api/samples/{sample_no}")
    assert response.status_code in (200, 404)


def test_reload_after_restore_keeps_review_state(page: Page):
    sample_no = f"RLOAD-{int(time.time() * 1000)}"
    seed(sample_no, "ST-RL", "2026-11-16T20:00:00Z", 7.3)
    page.click("[data-testid='refresh-warehouse']")
    page.locator(".warehouse-item", has_text=sample_no).locator("button").click()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("已入仓")

    document = backup_document()
    assert restore_from(document).status_code == 200
    page.click("[data-testid='refresh-warehouse']")
    expect(page.locator("[data-testid='rebase-panel']")).to_be_visible()

    # An offline close/reopen keeps the pending review.
    page.context.set_offline(True)
    page.reload(wait_until="domcontentloaded")
    expect(page.locator("[data-testid='sample-no']")).to_have_value(sample_no)
    expect(page.locator("[data-testid='rebase-panel']")).to_be_visible()
    expect(page.locator("[data-testid='status-badge']")).to_have_text("需核对")

    # Reconnecting on a new epoch does not resolve it without the user.
    page.context.set_offline(False)
    expect(page.locator("[data-testid='connection-text']")).to_have_text("在线")
    expect(page.locator("[data-testid='rebase-panel']")).to_be_visible()
