"""Authenticated HTTP interface for the Valkey Gemini quota scheduler."""

from __future__ import annotations

import hmac
import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from redis.asyncio import Redis
from redis.exceptions import RedisError

from quota_api.scheduler import ApiResult, QuotaScheduler


LOGGER = logging.getLogger(__name__)


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConfigureRequest(StrictRequest):
    keyGroups: list[str]
    rpmPerProject: float
    rpdPerProject: int


class LeaseRequest(StrictRequest):
    requestId: str | None = None


class ReportRequest(StrictRequest):
    leaseId: str
    httpStatus: int
    cooldownSeconds: float = 0
    dailyExhausted: bool = False
    quotaType: str = "unknown"


def create_app(
    scheduler: QuotaScheduler | None = None,
    *,
    token: str | None = None,
) -> FastAPI:
    token = token if token is not None else os.environ.get("QUOTA_API_TOKEN", "")
    if len(token) < 32:
        raise ValueError("QUOTA_API_TOKEN must contain at least 32 characters")
    owns_client = scheduler is None
    if scheduler is None:
        client = Redis.from_url(
            os.environ.get("VALKEY_URL", "redis://127.0.0.1:6379/0"),
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
            health_check_interval=30,
        )
        scheduler = QuotaScheduler(client)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await scheduler.client.ping()
        try:
            yield
        finally:
            if owns_client:
                await scheduler.client.aclose()

    app = FastAPI(
        title="Gemini quota API",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not hmac.compare_digest(supplied, token):
            raise HTTPException(status_code=401, detail="unauthorized")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException):
        return JSONResponse(status_code=error.status_code, content={"error": error.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError):
        return JSONResponse(status_code=400, content={"error": "invalid_request"})

    @app.exception_handler(RedisError)
    async def redis_error(_request: Request, error: RedisError):
        LOGGER.error("quota backend unavailable: %s", type(error).__name__)
        return JSONResponse(status_code=503, content={"error": "quota_backend_unavailable"})

    def response(result: ApiResult) -> JSONResponse:
        return JSONResponse(
            status_code=result.status,
            content=result.body,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/healthz")
    async def healthz():
        await scheduler.client.ping()
        return {"ok": True}

    @app.post("/v1/configure", dependencies=[Depends(authenticate)])
    async def configure(body: ConfigureRequest):
        return response(await scheduler.configure(
            body.keyGroups,
            rpm_per_project=body.rpmPerProject,
            rpd_per_project=body.rpdPerProject,
        ))

    @app.post("/v1/lease", dependencies=[Depends(authenticate)])
    async def lease(body: LeaseRequest):
        return response(await scheduler.lease(body.requestId))

    @app.post("/v1/report", dependencies=[Depends(authenticate)])
    async def report(body: ReportRequest):
        return response(await scheduler.report(
            body.leaseId,
            body.httpStatus,
            cooldown_seconds=body.cooldownSeconds,
            daily_exhausted=body.dailyExhausted,
            quota_type=body.quotaType,
        ))

    @app.post("/v1/status", dependencies=[Depends(authenticate)])
    async def status():
        return response(await scheduler.status())

    return app
