"""Domain model and validation shared by the HTTP API."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime

SAMPLE_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
STATION_NO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,31}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{8,128}$")

MAX_VERSION = 2_147_483_647


class ValidationError(ValueError):
    """Raised when a client payload is malformed."""


def json_error(message: str) -> dict[str, str]:
    return {"error": message}


def validate_sample_no(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("sample_no is required")
    value = value.strip()
    if not SAMPLE_NO_RE.fullmatch(value):
        raise ValidationError(
            "sample_no must be 1-64 chars of letters, digits, '.', '_' or '-'"
        )
    return value


def validate_station_no(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("station_no is required")
    value = value.strip()
    if not STATION_NO_RE.fullmatch(value):
        raise ValidationError(
            "station_no must be 1-32 chars of letters, digits, '.', '_', '/' or '-'"
        )
    return value


def validate_ph(value: object) -> float:
    # bool is a subclass of int; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("ph must be a number between 0 and 14")
    number = float(value)
    if number < 0.0 or number > 14.0:
        raise ValidationError("ph must be between 0 and 14")
    if not math.isfinite(number):
        raise ValidationError("ph must be between 0 and 14")
    # Keep a stable, finite representation (SQLite stores REAL).
    return round(number, 4)


def validate_sampled_at(value: object) -> str:
    """Accept ISO-8601 with an explicit UTC offset and normalise to UTC 'Z'."""
    if not isinstance(value, str):
        raise ValidationError("sampled_at must be an ISO-8601 timestamp with an offset")
    raw = value.strip()
    if not raw:
        raise ValidationError("sampled_at must not be empty")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("sampled_at must be a valid ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("sampled_at must include a UTC offset (for example Z)")
    utc = parsed.astimezone(UTC)
    # Canonical form, second precision is enough for field sampling.
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_version(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError("base_version must be an integer")
    if value < 0 or value > MAX_VERSION:
        raise ValidationError("base_version is out of range")
    return value


def payload_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
