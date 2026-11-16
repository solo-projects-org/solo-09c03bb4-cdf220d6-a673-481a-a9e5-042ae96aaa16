"""Backup file format: export, checksum and strict validation.

A backup is a self-contained JSON document.  ``checksum`` is the SHA-256 of a
canonical serialisation of every other field, so any edit to the file is
detected before the warehouse is touched.  Validation is intentionally strict:
a malformed backup must be rejected *without* modifying the live database.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

from .models import (
    MAX_VERSION,
    ValidationError,
    validate_ph,
    validate_sample_no,
    validate_sampled_at,
    validate_station_no,
)

BACKUP_FORMAT = "river-sampling-backup"
BACKUP_FORMAT_VERSION = 1
MAX_BACKUP_BYTES = 32 * 1024 * 1024
MAX_BACKUP_SAMPLES = 200_000
CANONICAL_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class InvalidBackupError(ValueError):
    """Raised when an uploaded file is not a valid same-site backup."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def checksum_of(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "checksum"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def build_backup(
    *,
    site_id: str,
    meta: dict[str, Any],
    samples: list[dict[str, Any]],
    exported_at: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "site_id": site_id,
        "epoch": meta["epoch"],
        "generation": int(meta["generation"]),
        "exported_at": exported_at,
        "sample_count": len(samples),
        "samples": [
            {
                "sample_no": row["sample_no"],
                "station_no": row["station_no"],
                "sampled_at": row["sampled_at"],
                "ph": row["ph"],
                "version": int(row["version"]),
                "updated_at": row["updated_at"],
            }
            for row in samples
        ],
    }
    payload["checksum"] = checksum_of(payload)
    return payload


def _invalid(message: str) -> InvalidBackupError:
    return InvalidBackupError(message)


def _validate_time(value: Any, field: str) -> str:
    if not isinstance(value, str) or not CANONICAL_TIME_RE.fullmatch(value):
        raise _invalid(f"{field} must be a UTC timestamp like YYYY-MM-DDTHH:MM:SSZ")
    try:
        normalized = validate_sampled_at(value)
    except ValidationError as exc:
        raise _invalid(f"{field}: {exc}") from exc
    if normalized != value:
        raise _invalid(f"{field} is not in canonical UTC form")
    return normalized


def _validate_sample(item: Any, index: int) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _invalid(f"samples[{index}] must be an object")
    try:
        sample_no = validate_sample_no(item.get("sample_no"))
        station_no = validate_station_no(item.get("station_no"))
        sampled_at = validate_sampled_at(item.get("sampled_at"))
        ph = validate_ph(item.get("ph"))
    except ValidationError as exc:
        raise _invalid(f"samples[{index}]: {exc}") from exc
    if sampled_at != item.get("sampled_at"):
        raise _invalid(f"samples[{index}].sampled_at is not canonical UTC")
    version = item.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise _invalid(f"samples[{index}].version must be an integer")
    if version < 1 or version > MAX_VERSION:
        raise _invalid(f"samples[{index}].version is out of range")
    updated_at = _validate_time(item.get("updated_at"), f"samples[{index}].updated_at")
    return {
        "sample_no": sample_no,
        "station_no": station_no,
        "sampled_at": sampled_at,
        "ph": ph,
        "version": version,
        "updated_at": updated_at,
    }


def validate_backup(raw: Any, *, site_id: str) -> dict[str, Any]:
    """Validate an uploaded backup and return normalised content.

    Raises :class:`InvalidBackupError` for anything suspicious.  The caller
    must not start a write transaction before this returns.
    """
    if not isinstance(raw, dict):
        raise _invalid("backup must be a JSON object")
    if raw.get("format") != BACKUP_FORMAT:
        raise _invalid("not a river-sampling backup file")
    format_version = raw.get("format_version")
    if (
        isinstance(format_version, bool)
        or not isinstance(format_version, int)
        or format_version != BACKUP_FORMAT_VERSION
    ):
        raise _invalid(
            f"unsupported backup format_version (expected {BACKUP_FORMAT_VERSION})"
        )
    if raw.get("site_id") != site_id:
        raise _invalid("backup belongs to a different site")
    generation = raw.get("generation")
    valid_generation = isinstance(generation, int) and not isinstance(generation, bool)
    if not valid_generation or generation < 1:
        raise _invalid("generation must be a positive integer")
    epoch = raw.get("epoch")
    if not isinstance(epoch, str) or not epoch.strip():
        raise _invalid("epoch is missing")
    exported_at = raw.get("exported_at")
    if not isinstance(exported_at, str) or not exported_at.strip():
        raise _invalid("exported_at is missing")
    samples = raw.get("samples")
    if not isinstance(samples, list):
        raise _invalid("samples must be an array")
    if len(samples) > MAX_BACKUP_SAMPLES:
        raise _invalid("backup contains too many samples")
    sample_count = raw.get("sample_count")
    if (
        isinstance(sample_count, bool)
        or not isinstance(sample_count, int)
        or sample_count != len(samples)
    ):
        raise _invalid("sample_count does not match the sample list")

    checksum = raw.get("checksum")
    if not isinstance(checksum, str) or not hmac.compare_digest(
        checksum, checksum_of(raw)
    ):
        raise _invalid("checksum mismatch (the backup file is corrupted or edited)")

    normalised: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(samples):
        sample = _validate_sample(item, index)
        if sample["sample_no"] in seen:
            raise _invalid(f"duplicate sample_no {sample['sample_no']}")
        seen.add(sample["sample_no"])
        normalised.append(sample)

    return {
        "format": BACKUP_FORMAT,
        "format_version": BACKUP_FORMAT_VERSION,
        "site_id": site_id,
        "epoch": epoch,
        "generation": generation,
        "exported_at": exported_at,
        "sample_count": len(normalised),
        "samples": normalised,
        "checksum": checksum,
    }
