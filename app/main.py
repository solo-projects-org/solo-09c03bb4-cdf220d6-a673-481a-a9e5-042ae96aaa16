"""FastAPI application exposing the water-sample warehouse API."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

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
    RequestReuseError,
    get_sample,
    list_samples,
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


def create_app(database_path: str | None = None) -> FastAPI:
    db_path = database_path or os.environ.get("DATABASE_PATH", "/data/samples.db")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(db_path)
        yield

    app = FastAPI(
        title="河段水质采样",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/samples")
    def api_list_samples() -> JSONResponse:
        return JSONResponse(
            content={"samples": list_samples(db_path)},
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
            content={"sample": sample}, headers={"Cache-Control": "no-store"}
        )

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
            )
        except ConflictError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "conflict",
                    "message": "warehouse copy has a different version",
                    "local": exc.local,
                    "server": exc.server,
                },
            )
        except RequestReuseError as exc:
            return JSONResponse(status_code=422, content={"error": str(exc)})

        response = JSONResponse(status_code=status, content=body)
        response.headers["X-Idempotent-Replay"] = "true" if replayed else "false"
        response.headers["Cache-Control"] = "no-store"
        return response

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
