"""web-ui: веб-интерфейс складской системы (страницы в браузере).

Берёт: действия пользователя в браузере (открыть страницу, отправить форму).
Делает: по схеме MVC (см. controllers.py) обращается к системе через API Gateway
  и показывает результат HTML-страницей. Токен входа хранит в cookie httpOnly
  (JavaScript страницы его не видит).
Получается: интерфейс для ролей admin, manager, picker: товары, ячейки и остатки, заказы,
  сборка, отгрузки, уведомления.
Своей базы и бизнес-логики у web-ui нет: все правила (резерв, роли, статусы) проверяют
бизнес-сервисы, web-ui только показывает их ответы. Поэтому он такой же клиент gateway,
как Swagger или e2e-тест, и его можно заменить другим интерфейсом, не трогая backend.
"""
from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pad_common.logging import log_event, request_id_var, setup_logging

from .api_client import ApiError
from .config import Settings
from .controllers import router, templates
from .deps import NotLoggedIn, REFRESH_COOKIE, TOKEN_COOKIE
from .view_helpers import details_text, error_text

log = logging.getLogger("web-ui")
VERSION = "1.0.0"


def build_app(settings: Settings | None = None, transport: httpx.BaseTransport | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.service_name, settings.log_level, settings.log_format)
        log_event(log, "web-ui started", gateway=settings.gateway_url)
        yield
        app.state.http.close()

    app = FastAPI(title="Web UI", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    # Один HTTP-клиент на всё приложение: соединения с gateway переиспользуются.
    app.state.http = httpx.Client(transport=transport, timeout=settings.http_timeout)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    app.include_router(router)

    @app.middleware("http")
    async def context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        if not request.url.path.startswith(("/static", "/health", "/ready")):
            log_event(log, "request", method=request.method, path=request.url.path, status=response.status_code,
                      ms=round((time.perf_counter() - start) * 1000, 1))
        request_id_var.reset(token)
        return response

    @app.exception_handler(NotLoggedIn)
    async def not_logged_in(request: Request, exc: NotLoggedIn):
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        """Одна точка обработки ошибок API для всех страниц.

        401 (нет входа, токен истёк) -> страница входа с причиной.
        Ошибка после отправки формы -> назад на ту же страницу с текстом ошибки (?err=...).
        Ошибка при открытии страницы -> страница с текстом ошибки.
        """
        text = error_text(exc.code, exc.message)
        if exc.details:
            text += ": " + details_text(exc.details)
        log_event(log, "api error", logging.WARNING, status=exc.status, code=exc.code, path=request.url.path)
        if exc.status == 401:
            resp = RedirectResponse("/login?err=" + quote(text), status_code=303)
            resp.delete_cookie(TOKEN_COOKIE)
            resp.delete_cookie(REFRESH_COOKIE)
            return resp
        if request.method == "POST":
            back = urlparse(request.headers.get("referer", "")).path or "/"
            return RedirectResponse(back + "?err=" + quote(text), status_code=303)
        return templates.TemplateResponse(request, "error.html", {"user": None, "message": text, "status": exc.status},
                                          status_code=exc.status if exc.status >= 400 else 500)

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok", "service": settings.service_name, "version": VERSION}

    @app.get("/ready", include_in_schema=False)
    def ready(request: Request):
        """Готов, если отвечает gateway: без него интерфейс бесполезен."""
        try:
            ok = request.app.state.http.get(settings.gateway_url + "/health", timeout=2.0).status_code == 200
            detail = "gateway ok" if ok else "gateway error"
        except httpx.HTTPError as exc:
            ok, detail = False, type(exc).__name__
        return JSONResponse({"status": "ready" if ok else "not_ready", "service": settings.service_name,
                             "checks": {"gateway": {"ok": ok, "detail": detail}}}, status_code=200 if ok else 503)

    return app


app = build_app()
