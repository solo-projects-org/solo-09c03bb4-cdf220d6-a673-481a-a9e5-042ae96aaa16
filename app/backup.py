"""Portable, checksummed warehouse backup format.

A backup is a single JSON document that contains every sample of one site plus
enough identity to refuse a backup from another site or another format
version. Downloads are produced from one read snapshot; uploads are validated
completely in memory before a restore transaction is opened, so a malformed
file can never leave the live warehouse half-written.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from .models import (
    BACKUP_FORMAT,
    BACKUP_FORMAT_VERSION,
    CHECKSUM_RE,
    MAX_BACKUP_SAMPLES,
    MAX_VERSION,
    UTC_SECOND_RE,
    ValidationError,
    payload_hash,
    validate_ph,
    validate_sample_no,
    validate_sampled_at,
    validate_station_no,
)

SAMPLE_KEYS = {
    "sample_no",
    "station_no",
    "sampled_at",
    "ph",
    "version",
    "updated_at",
}
BACKUP_KEYS = {
    "format",
    "format_version",
    "site_id",
    "created_at",
    "epoch",
    "restore_count",
    "sample_count",
    "samples",
    "checksum",
}
MAX_BACKUP_BYTES = 32 * 1024 * 1024


class BackupError(ValueError):
    """Raised when an uploaded backup is not a valid backup of this site."""


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_backup(
    *,
    site_id: str,
    epoch: int,
    restore_count: int,
    samples: list[dict[str, Any]],
    created_at: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "site_id": site_id,
        "created_at": created_at or now_utc(),
        "epoch": epoch,
        "restore_count": restore_count,
        "sample_count": len(samples),
        "samples": samples,
    }
    body["checksum"] = payload_hash(body)
    return body


def _require_utc(value: object, field: str) -> str:
    if not isinstance(value, str) or not UTC_SECOND_RE.fullmatch(value):
        raise BackupError(f"{field} must be a UTC timestamp like 2026-01-01T00:00:00Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError(f"{field} must be a valid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BackupError(f"{field} must be a valid UTC timestamp")
    return value


def _require_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BackupError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise BackupError(f"{field} is out of range")
    return value


def _parse_sample(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise BackupError("each sample must be an object")
    unknown = set(raw) - SAMPLE_KEYS
    missing = SAMPLE_KEYS - set(raw)
    if unknown or missing:
        raise BackupError(
            "each sample must contain exactly " + ", ".join(sorted(SAMPLE_KEYS))
        )
    try:
        sample_no = validate_sample_no(raw["sample_no"])
        station_no = validate_station_no(raw["station_no"])
        sampled_at = validate_sampled_at(raw["sampled_at"])
        ph = validate_ph(raw["ph"])
    except ValidationError as exc:
        raise BackupError(f"sample {raw.get('sample_no')!r}: {exc}") from exc
    version = _require_int(raw["version"], "sample.version", 1, MAX_VERSION)
    updated_at = _require_utc(raw["updated_at"], "sample.updated_at")
    return {
        "sample_no": sample_no,
        "station_no": station_no,
        "sampled_at": sampled_at,
        "ph": ph,
        "version": version,
        "updated_at": updated_at,
    }


def parse_backup(raw: bytes, *, expected_site_id: str) -> dict[str, Any]:
    """Validate a backup completely; raise BackupError without touching the DB."""
    if len(raw) > MAX_BACKUP_BYTES:
        raise BackupError("backup file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BackupError("backup must be UTF-8 encoded JSON") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BackupError("backup is not valid JSON") from exc
    if not isinstance(document, dict):
        raise BackupError("backup must be a JSON object")
    if set(document) != BACKUP_KEYS:
        raise BackupError(
            "backup must contain exactly: " + ", ".join(sorted(BACKUP_KEYS))
        )
    if document["format"] != BACKUP_FORMAT:
        raise BackupError("unsupported backup format")
    if document["format_version"] != BACKUP_FORMAT_VERSION:
        raise BackupError(
            f"unsupported backup version {document['format_version']!r}; "
            f"expected {BACKUP_FORMAT_VERSION}"
        )
    site_id = document["site_id"]
    if not isinstance(site_id, str) or not site_id:
        raise BackupError("backup site_id must be a non-empty string")
    if site_id != expected_site_id:
        raise BackupError(
            f"backup belongs to site {site_id!r}, "
            f"this warehouse is {expected_site_id!r}"
        )
    _require_utc(document["created_at"], "created_at")

    checksum = document["checksum"]
    if not isinstance(checksum, str) or not CHECKSUM_RE.fullmatch(checksum):
        raise BackupError("backup checksum is missing or malformed")
    body = {key: value for key, value in document.items() if key != "checksum"}
    if payload_hash(body) != checksum:
        raise BackupError("backup checksum does not match its content")

    _require_int(document["epoch"], "epoch", 0, 2_147_483_647)
    _require_int(document["restore_count"], "restore_count", 0, 2_147_483_647)

    raw_samples = document["samples"]
    if not isinstance(raw_samples, list):
        raise BackupError("backup samples must be a list")
    if len(raw_samples) > MAX_BACKUP_SAMPLES:
        raise BackupError("backup contains too many samples")
    sample_count = _require_int(
        document["sample_count"], "sample_count", 0, MAX_BACKUP_SAMPLES
    )
    if sample_count != len(raw_samples):
        raise BackupError("sample_count does not match the sample list")

    samples = [_parse_sample(item) for item in raw_samples]
    seen: set[str] = set()
    for sample in samples:
        if sample["sample_no"] in seen:
            raise BackupError(f"duplicate sample_no {sample['sample_no']!r}")
        seen.add(sample["sample_no"])
    return {"site_id": site_id, "samples": samples}
