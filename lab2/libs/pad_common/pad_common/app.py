"""Заготовка FastAPI-приложения, одинаковая для всех сервисов.

Берёт: настройки сервиса, его таблицы (metadata) и название.
Делает:
  * логи в JSON с request_id; заголовок X-Request-ID в каждом ответе;
  * единый формат ошибок без stack trace (pad_common.errors);
  * GET /health  - процесс жив (liveness), ничего не проверяет, отвечает сразу;
  * GET /ready   - сервис может принимать запросы (readiness): база отвечает; иначе 503.
                   Соединение с RabbitMQ показывается в ответе, но не обязательно: без брокера
                   ответ 200 со статусом degraded (события ждут в outbox);
  * при старте создаёт таблицы (с повторами, пока PostgreSQL поднимается) и запускает EventBus.
Получается: объект FastAPI, к которому сервис добавляет свои роутеры.
Зачем: семь сервисов ведут себя одинаково, а различаются только бизнес-логикой.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Callable

import asyncio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .auth import PublicKeyProvider
from .config import BaseServiceSettings
from .db import Base, Database
from .errors import AppError, error_body
from .events import EventBus
from .logging import log_event, request_id_var, setup_logging

log = logging.getLogger("service")

ReadyCheck = Callable[[], tuple[bool, str]]


def create_service(settings: BaseServiceSettings, *, title: str, description: str, version: str = "1.0.0",
                   metadata=None, on_startup: Callable[[FastAPI], None] | None = None) -> FastAPI:
    db = Database(settings.database_url, metadata if metadata is not None else Base.metadata)
    bus = EventBus(settings.rabbitmq_url, settings.events_exchange, settings.service_name, db)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.service_name, settings.log_level, settings.log_format)
        await _create_tables(db)
        if on_startup:
            on_startup(app)
        await bus.start()
        log_event(log, "service started", version=version, database=db.engine.dialect.name, events=bus.enabled)
        yield
        await bus.stop()
        log.info("service stopped")

    app = FastAPI(title=title, description=description, version=version, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.bus = bus
    app.state.key_provider = PublicKeyProvider(settings)
    app.state.ready_checks = {}  # имя -> функция () -> (ok, detail); сервис может добавить свои

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001 - последняя линия обороны
            log.exception("unhandled error")
            response = JSONResponse(error_body("internal_error", "internal error", rid), status_code=500)
        response.headers["X-Request-ID"] = rid
        if request.url.path not in ("/health", "/ready"):
            log_event(log, "request", method=request.method, path=request.url.path, status=response.status_code,
                      ms=round((time.perf_counter() - start) * 1000, 1))
        request_id_var.reset(token)
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        if exc.status >= 500:
            log_event(log, "request failed", logging.WARNING, code=exc.code, error=exc.message)
        return JSONResponse(error_body(exc.code, exc.message, request_id_var.get(), exc.details), status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        details = [{"field": ".".join(str(p) for p in e["loc"][1:]), "problem": e["msg"]} for e in exc.errors()]
        return JSONResponse(error_body("invalid_request", "request validation failed", request_id_var.get(), details), status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(error_body(code, str(exc.detail), request_id_var.get()), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(error_body("internal_error", "internal error", request_id_var.get()), status_code=500)

    @app.get("/health", tags=["service"], summary="Liveness: процесс жив")
    def health():
        return {"status": "ok", "service": settings.service_name, "version": version}

    @app.get("/ready", tags=["service"], summary="Readiness: база и брокер доступны")
    def ready():
        checks: dict = {}
        ok_db, detail = db.ping()
        checks["database"] = {"ok": ok_db, "detail": detail}
        if bus.enabled:
            # RabbitMQ показывается, но не делает сервис неготовым: события пишутся в outbox и
            # уйдут, когда брокер вернётся. Иначе при падении брокера Kubernetes убрал бы из
            # Service все копии order/inventory/fulfillment/notification, и система перестала бы
            # отвечать даже на запросы, которым брокер не нужен (лаба 3, DECISIONS.md).
            connected = bus.is_connected()
            checks["rabbitmq"] = {"ok": connected, "required": False,
                                  "detail": "connected" if connected else "not connected, events wait in outbox"}
        for name, fn in app.state.ready_checks.items():
            ok, d = fn()
            checks[name] = {"ok": ok, "detail": d}
        ok = all(c["ok"] for c in checks.values() if c.get("required", True))
        degraded = ok and not all(c["ok"] for c in checks.values())
        status = "degraded" if degraded else ("ready" if ok else "not_ready")
        return JSONResponse({"status": status, "service": settings.service_name, "checks": checks},
                            status_code=200 if ok else 503)

    return app


async def _create_tables(db: Database, attempts: int = 30) -> None:
    """Создать таблицы. PostgreSQL в Docker может подниматься дольше сервиса, поэтому повторяем."""
    for i in range(1, attempts + 1):
        try:
            await asyncio.to_thread(db.create_all)
            return
        except Exception as exc:  # noqa: BLE001
            if i == attempts:
                raise
            log_event(log, "database not available yet, retrying", logging.WARNING, attempt=i, error=type(exc).__name__)
            await asyncio.sleep(min(2.0, 0.2 * i))


def get_session(request: Request):
    """Зависимость FastAPI: одна транзакция базы на один запрос (commit в конце, rollback при ошибке)."""
    with request.app.state.db.session() as s:
        yield s
