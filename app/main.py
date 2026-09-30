"""博物馆展柜温度暴露审定服务 HTTP API。"""

from __future__ import annotations

import sqlite3

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import service
from .db import get_conn, init_db
from .schemas import (
    CertificateOut,
    EvaluationOut,
    ReadingOut,
    ReadingSubmit,
    ReadingSubmitResult,
    WindowCreate,
    WindowOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="博物馆展柜温度暴露审定服务",
    version="1.0.0",
    description="基于 FastAPI + SQLite 的展柜温度暴露审定 / 签发服务",
    lifespan=lifespan,
)


@app.exception_handler(service.DomainError)
def _domain_error_handler(request: Request, exc: service.DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
def _validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error", "message": "请求参数校验失败",
                           "details": {"errors": exc.errors()}}},
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# 审定窗口
# ---------------------------------------------------------------------------

@app.post("/windows", response_model=WindowOut, status_code=201, tags=["windows"])
def create_window(payload: WindowCreate, conn: sqlite3.Connection = Depends(get_conn)):
    return service.create_window(conn, payload)


@app.get("/windows", response_model=list[WindowOut], tags=["windows"])
def list_windows(conn: sqlite3.Connection = Depends(get_conn)):
    return service.list_windows(conn)


@app.get("/windows/{window_id}", response_model=WindowOut, tags=["windows"])
def get_window(window_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_window(conn, window_id)


# ---------------------------------------------------------------------------
# 读数
# ---------------------------------------------------------------------------

@app.post(
    "/windows/{window_id}/readings",
    response_model=ReadingSubmitResult,
    status_code=201,
    tags=["readings"],
)
def submit_reading(
    window_id: str,
    payload: ReadingSubmit,
    response: Response,
    conn: sqlite3.Connection = Depends(get_conn),
):
    result = service.submit_reading(conn, window_id, payload)
    # 相同 ID + 修订号重复提交：幂等返回 200；新提交 / 替换返回 201。
    if result["status"] == "idempotent":
        response.status_code = 200
    return result


@app.get("/windows/{window_id}/readings", response_model=list[ReadingOut], tags=["readings"])
def list_readings(
    window_id: str,
    include_superseded: bool = Query(False, description="是否包含已被高修订号替换的历史读数"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    return service.list_readings(conn, window_id, include_superseded)


# ---------------------------------------------------------------------------
# 审定与签发
# ---------------------------------------------------------------------------

@app.get("/windows/{window_id}/evaluation", response_model=EvaluationOut, tags=["evaluation"])
def evaluate_window(window_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return service.evaluate_window(conn, window_id)


@app.post(
    "/windows/{window_id}/certificates",
    response_model=CertificateOut,
    status_code=201,
    tags=["certificates"],
)
def issue_certificate(window_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return service.issue_certificate(conn, window_id)


@app.get("/windows/{window_id}/certificates", response_model=list[CertificateOut], tags=["certificates"])
def list_certificates(window_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return service.list_certificates(conn, window_id)


@app.get(
    "/windows/{window_id}/certificates/{version}",
    response_model=CertificateOut,
    tags=["certificates"],
)
def get_certificate(window_id: str, version: int, conn: sqlite3.Connection = Depends(get_conn)):
    return service.get_certificate(conn, window_id, version)
