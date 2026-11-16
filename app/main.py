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

from .backup import MAX_BACKUP_BYTES, BackupError
from .db import init_db
from .models import (
    REQUEST_ID_RE,
    ValidationError,
    parse_epoch,
    parse_generation,
    parse_version,
    validate_ph,
    validate_sample_no,
    validate_sampled_at,
    validate_station_no,
)
from .store import (
    ConflictError,
    RequestReuseError,
    RestoreConflictError,
    RestoreLimitError,
    export_backup,
    get_meta,
    get_sample,
    read_warehouse,
    restore_backup,
    upsert_sample,
)

STATIC_DIR = Path(__file__).parent / "static"


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    station_no: Any = None
    sampled_at: Any = None
    ph: Any = None
    base_version: Any = 0
    request_id: Any = None
    base_epoch: Any = None
    generation: Any = None


def create_app(database_path: str | None = None) -> FastAPI:
    db_path = database_path or os.environ.get("DATABASE_PATH", "/data/samples.db")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(db_path, site_id=os.environ.get("SITE_ID"))
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
    def api_warehouse() -> JSONResponse:
        snapshot = read_warehouse(db_path)
        return JSONResponse(
            content=snapshot,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/samples")
    def api_list_samples() -> JSONResponse:
        snapshot = read_warehouse(db_path)
        return JSONResponse(
            content={
                "samples": snapshot["samples"],
                "warehouse": snapshot["warehouse"],
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/samples/{sample_no}")
    def api_get_sample(sample_no: str) -> JSONResponse:
        try:
            valid_no = validate_sample_no(sample_no)
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})
        sample = get_sample(db_path, valid_no)
        if sample is None:
            return JSONResponse(status_code=404, content={"error": "sample not found"})
        return JSONResponse(
            content={"sample": sample, "warehouse": get_meta_public(db_path)},
            headers={"Cache-Control": "no-store"},
        )

    @app.put("/api/samples/{sample_no}")
    def api_put_sample(sample_no: str, payload: SampleIn) -> JSONResponse:
        try:
            valid_no = validate_sample_no(sample_no)
            station_no = validate_station_no(payload.station_no)
            sampled_at = validate_sampled_at(payload.sampled_at)
            ph = validate_ph(payload.ph)
            base_version = parse_version(payload.base_version)
            base_epoch = parse_epoch(payload.base_epoch)
            generation = parse_generation(payload.generation)
            request_id = payload.request_id
            if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(
                request_id
            ):
                raise ValidationError(
                    "request_id must be 8-128 chars of letters, digits, "
                    "'.', '_', ':' or '-'"
                )
        except ValidationError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})

        try:
            status, body, replayed = upsert_sample(
                db_path,
                sample_no=valid_no,
                station_no=station_no,
                sampled_at=sampled_at,
                ph=ph,
                base_version=base_version,
                request_id=request_id,
                base_epoch=base_epoch,
                generation=generation,
            )
        except RestoreConflictError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "restore_conflict",
                    "message": (
                        "warehouse was restored after this edit was prepared; "
                        "rebase the local copy on the restored warehouse"
                    ),
                    "local": exc.local,
                    "base_epoch": exc.base_epoch,
                    "warehouse": exc.warehouse,
                },
            )
        except ConflictError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "conflict",
                    "message": "warehouse copy has a different version",
                    "local": exc.local,
                    "server": exc.server,
                    "warehouse": get_meta_public(db_path),
                },
            )
        except RequestReuseError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})

        response = JSONResponse(status_code=status, content=body)
        response.headers["X-Idempotent-Replay"] = "true" if replayed else "false"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/backup")
    def api_download_backup() -> Response:
        document = export_backup(db_path)
        filename = f"river-sampling-{document['site_id']}-epoch{document['epoch']}.json"
        return Response(
            content=json.dumps(
                document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            media_type="application/json",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.post("/api/restore")
    async def api_restore_backup(request: Request) -> JSONResponse:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BACKUP_BYTES:
            return JSONResponse(
                status_code=413,
                content={
                    "error": "invalid_backup",
                    "message": "backup file is too large",
                },
            )
        raw = await request.body()
        try:
            summary = restore_backup(db_path, raw)
        except BackupError as exc:
            return JSONResponse(
                status_code=422,
                content={
                    "error": "invalid_backup",
                    "message": str(exc),
                },
                headers={"Cache-Control": "no-store"},
            )
        except RestoreLimitError as exc:
            return JSONResponse(
                status_code=409,
                content={"error": "restore_limit", "message": str(exc)},
            )
        snapshot = read_warehouse(db_path)
        return JSONResponse(
            status_code=200,
            content={"restored": summary, "warehouse": snapshot["warehouse"]},
            headers={"Cache-Control": "no-store"},
        )

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

    def get_meta_public(path: str) -> dict[str, Any]:
        meta = get_meta(path)
        return {
            "site_id": meta["site_id"],
            "epoch": meta["epoch"],
            "generation": meta["generation"],
            "restored_at": meta["restored_at"],
            "restore_count": meta["restore_count"],
        }

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
