"""FastAPI application exposing the water-sample warehouse API."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .backup import (
    MAX_BACKUP_BYTES,
    InvalidBackupError,
    canonical_json,
    validate_backup,
)
from .db import init_db
from .models import (
    REQUEST_ID_RE,
    ValidationError,
    parse_version,
    validate_ph,
    validate_sample_no,
    validate_sampled_at,
    validate_station_no,
)
from .store import (
    ConflictError,
    EpochMismatchError,
    EpochRequiredError,
    InvalidRestore,
    RequestReuseError,
    export_backup,
    get_sample,
    get_warehouse_meta,
    restore_backup,
    snapshot_samples,
    upsert_sample,
)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_SITE_ID = "river-sampling-site"


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    station_no: Any = None
    sampled_at: Any = None
    ph: Any = None
    base_version: Any = 0
    request_id: Any = None
    # Optional: the warehouse epoch the client last saw.  A mismatch means the
    # data set was restored since then, so the submission is refused.
    epoch: Any = None


def _json_no_store(status_code: int, content: dict[str, Any]) -> JSONResponse:
    return JSONResponse(
        status_code=status_code, content=content, headers={"Cache-Control": "no-store"}
    )


def create_app(
    database_path: str | None = None, site_id: str | None = None
) -> FastAPI:
    db_path = database_path or os.environ.get("DATABASE_PATH", "/data/samples.db")
    site = site_id or os.environ.get("SITE_ID", DEFAULT_SITE_ID)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(db_path, site)
        yield

    app = FastAPI(
        title="河段水质采样",
        version="1.1.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/warehouse")
    def api_warehouse_meta() -> JSONResponse:
        meta = get_warehouse_meta(db_path)
        return _json_no_store(
            200,
            {
                "site_id": site,
                "epoch": meta["epoch"],
                "generation": meta["generation"],
                "created_at": meta.get("created_at"),
                "restored_at": meta.get("restored_at"),
                "sample_count": meta["sample_count"],
                "last_restore": meta.get("last_restore"),
            },
        )

    @app.get("/api/samples")
    def api_list_samples() -> JSONResponse:
        return _json_no_store(200, snapshot_samples(db_path))

    @app.get("/api/samples/{sample_no}")
    def api_get_sample(sample_no: str) -> JSONResponse:
        try:
            valid_no = validate_sample_no(sample_no)
        except ValidationError as exc:
            return _json_no_store(422, {"error": str(exc)})
        sample = get_sample(db_path, valid_no)
        if sample is None:
            return _json_no_store(404, {"error": "sample not found"})
        return _json_no_store(200, {"sample": sample})

    @app.put("/api/samples/{sample_no}")
    def api_put_sample(sample_no: str, payload: SampleIn) -> JSONResponse:
        try:
            valid_no = validate_sample_no(sample_no)
            station_no = validate_station_no(payload.station_no)
            sampled_at = validate_sampled_at(payload.sampled_at)
            ph = validate_ph(payload.ph)
            base_version = parse_version(payload.base_version)
            request_id = payload.request_id
            if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(
                request_id
            ):
                raise ValidationError(
                    "request_id must be 8-128 chars of letters, digits, "
                    "'.', '_', ':' or '-'"
                )
            client_epoch = payload.epoch
            if client_epoch is not None and not isinstance(client_epoch, str):
                raise ValidationError("epoch must be a string")
        except ValidationError as exc:
            return _json_no_store(422, {"error": str(exc)})

        try:
            status, body, replayed, committed_epoch = upsert_sample(
                db_path,
                sample_no=valid_no,
                station_no=station_no,
                sampled_at=sampled_at,
                ph=ph,
                base_version=base_version,
                request_id=request_id,
                client_epoch=client_epoch or None,
            )
        except ConflictError as exc:
            response = _json_no_store(
                409,
                {
                    "error": "conflict",
                    "message": "warehouse copy has a different version",
                    "local": exc.local,
                    "server": exc.server,
                },
            )
            meta = get_warehouse_meta(db_path)
            response.headers["X-Warehouse-Epoch"] = meta["epoch"]
            response.headers["X-Warehouse-Generation"] = str(meta["generation"])
            return response
        except EpochMismatchError as exc:
            meta = get_warehouse_meta(db_path)
            return JSONResponse(
                status_code=409,
                content={
                    "error": "epoch_changed",
                    "message": "the warehouse was restored; refresh before uploading",
                    "client_epoch": exc.client_epoch,
                    "epoch": exc.server_epoch,
                    "generation": meta["generation"],
                    "restored_at": meta.get("restored_at"),
                },
                headers={
                    "Cache-Control": "no-store",
                    # Explicitly not a replay: a pre-restore success must never
                    # be reported as a post-restore success.
                    "X-Idempotent-Replay": "false",
                    "X-Warehouse-Epoch": exc.server_epoch,
                    "X-Warehouse-Current-Epoch": exc.server_epoch,
                },
            )
        except EpochRequiredError as exc:
            # A legacy client (or a request prepared before the restore) with
            # no era: refuse, but keep the sample-version contract intact.
            return JSONResponse(
                status_code=409,
                content={
                    "error": "epoch_required",
                    "message": "the warehouse was restored; refresh before uploading",
                    "epoch": exc.server_epoch,
                    "generation": exc.generation,
                },
                headers={
                    "Cache-Control": "no-store",
                    "X-Idempotent-Replay": "false",
                    "X-Warehouse-Epoch": exc.server_epoch,
                    "X-Warehouse-Current-Epoch": exc.server_epoch,
                },
            )
        except RequestReuseError as exc:
            return _json_no_store(422, {"error": str(exc)})

        response = JSONResponse(status_code=status, content=body)
        response.headers["X-Idempotent-Replay"] = "true" if replayed else "false"
        response.headers["Cache-Control"] = "no-store"
        # Two different eras matter here:
        #  * committed: the era the write belongs to (fixed at commit time);
        #  * current:   the era live when the response is produced, so a client
        #               can notice a restore that raced its request.
        current = get_warehouse_meta(db_path)
        response.headers["X-Warehouse-Epoch"] = committed_epoch
        response.headers["X-Warehouse-Current-Epoch"] = current["epoch"]
        response.headers["X-Warehouse-Generation"] = str(current["generation"])
        return response

    # ----------------------------------------------------------------- backup

    @app.get("/api/backup")
    def api_download_backup() -> Response:
        backup = export_backup(db_path, site)
        body = canonical_json(backup).encode("utf-8")
        stamp = backup["exported_at"].replace(":", "").replace("-", "")
        filename = f"river-samples-g{backup['generation']}-{stamp}.json"
        return Response(
            content=body,
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Backup-Generation": str(backup["generation"]),
                "X-Backup-Epoch": backup["epoch"],
            },
        )

    @app.post("/api/backup/restore")
    async def api_restore_backup(request: Request) -> JSONResponse:
        # Read with a hard cap: never buffer an unbounded upload.
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BACKUP_BYTES:
                return _json_no_store(
                    413, {"error": "backup file is too large"}
                )
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
        if not raw_bytes:
            return _json_no_store(422, {"error": "backup file is empty"})
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return _json_no_store(
                422, {"error": f"backup is not valid UTF-8 JSON: {exc}"}
            )

        # Validation happens before any write transaction: an invalid file
        # leaves the live database byte-for-byte untouched.
        try:
            backup = validate_backup(raw, site_id=site)
        except InvalidBackupError as exc:
            return _json_no_store(
                422,
                {
                    "error": "invalid_backup",
                    "message": str(exc),
                    "warehouse_unchanged": True,
                },
            )

        try:
            result = restore_backup(db_path, backup=backup, site_id=site)
        except InvalidRestore as exc:
            return _json_no_store(
                422,
                {
                    "error": "invalid_backup",
                    "message": str(exc),
                    "warehouse_unchanged": True,
                },
            )

        result["previous_epoch"] = backup["epoch"]
        result["sample_count"] = backup["sample_count"]
        return _json_no_store(200, result)

    @app.get("/sw.js", include_in_schema=False)
    def service_worker() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "sw.js",
            media_type="text/javascript",
            headers={
                "Cache-Control": "no-cache",
                "Service-Worker-Allowed": "/",
            },
        )

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(
            STATIC_DIR / "index.html",
            media_type="text/html",
            headers={"Cache-Control": "no-cache"},
        )

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
