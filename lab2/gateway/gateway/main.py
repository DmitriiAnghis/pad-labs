"""API Gateway: единая входная точка /api/...

Берёт: любой запрос клиента на /api/<раздел>/...
Делает (по порядку):
  1. ставит X-Request-ID (или берёт присланный клиентом);
  2. если путь не публичный, проверяет JWT открытым ключом auth-service -> иначе 401;
  3. ограничивает частоту запросов на пользователя -> иначе 429;
  4. по таблице routes.py пересылает запрос нужному сервису с таймаутом, передавая
     Authorization и X-Request-ID; заголовки X-User-* от клиента выбрасывает и ставит свои;
  5. ответ сервиса возвращает клиенту как есть; нет ответа -> 503, таймаут -> 504.
Получается: клиент знает один адрес; сервисы не открыты наружу.
Gateway не считается бизнес-сервисом: своей логики и базы у него нет.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from pad_common.auth import PublicKeyProvider
from pad_common.errors import AppError, Unauthorized, error_body
from pad_common.logging import log_event, request_id_var, setup_logging

from .config import Settings
from .ratelimit import RateLimiter
from .routes import PUBLIC, build_routes, match

log = logging.getLogger("gateway")
VERSION = "1.0.0"
HOP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "upgrade",
               "x-user-id", "x-user-roles", "x-user-name"}


def build_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings()
    routes = build_routes(settings)
    limiter = RateLimiter(settings.rate_limit_per_minute)
    keys = PublicKeyProvider(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        setup_logging(settings.service_name, settings.log_level, settings.log_format)
        app.state.client = httpx.AsyncClient(transport=transport, follow_redirects=False)
        log_event(log, "gateway started", routes=[r.prefix for r in routes])
        yield
        await app.state.client.aclose()

    app = FastAPI(title="API Gateway", version=VERSION, lifespan=lifespan,
                  description="Единая точка входа. Маршруты: /api/auth, /api/users, /api/products, /api/locations, "
                              "/api/stock, /api/reservations, /api/orders, /api/pickings, /api/waves, /api/shipments, "
                              "/api/notifications, /api/assistant. Swagger каждого сервиса: /docs на его порту.")
    app.state.keys = keys

    def err(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(error_body(code, message, request_id_var.get()), status_code=status, headers=headers)

    @app.middleware("http")
    async def context(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001
            log.exception("unhandled error")
            response = err(500, "internal_error", "internal error")
        response.headers["X-Request-ID"] = rid
        if request.url.path not in ("/health", "/ready"):
            log_event(log, "request", method=request.method, path=request.url.path, status=response.status_code,
                      ms=round((time.perf_counter() - start) * 1000, 1))
        request_id_var.reset(token)
        return response

    @app.get("/health", tags=["service"])
    def health():
        return {"status": "ok", "service": settings.service_name, "version": VERSION}

    @app.get("/ready", tags=["service"])
    async def ready():
        try:
            await asyncio.to_thread(keys.get)
            ok, detail = True, "public key loaded"
        except AppError as exc:
            ok, detail = False, exc.message
        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": {"auth_public_key": {"ok": ok, "detail": detail}}},
                            status_code=200 if ok else 503)

    @app.get("/api/status", tags=["service"], summary="Готовность всех сервисов (для демонстрации)")
    async def status(request: Request):
        targets = {}
        for r in routes:
            targets.setdefault(r.service, r.base_url)

        async def probe(name: str, url: str):
            try:
                resp = await request.app.state.client.get(f"{url}/ready", timeout=2.0)
                return name, {"ready": resp.status_code == 200, "http": resp.status_code}
            except httpx.HTTPError as exc:
                return name, {"ready": False, "error": type(exc).__name__}

        results = dict(await asyncio.gather(*(probe(n, u) for n, u in targets.items())))
        return {"all_ready": all(v["ready"] for v in results.values()), "services": results}

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    async def proxy(path: str, request: Request):
        m = match(routes, path)
        if m is None:
            return err(404, "not_found", f"no route for /api/{path}")
        route, upstream_path = m

        principal_key = request.client.host if request.client else "anonymous"
        extra: dict[str, str] = {}
        if (request.method, path.rstrip("/")) not in PUBLIC:
            header = request.headers.get("Authorization", "")
            scheme, _, token = header.partition(" ")
            if scheme.lower() != "bearer" or not token:
                return err(401, "unauthorized", "missing bearer token")
            try:
                claims = await asyncio.to_thread(keys.decode, token.strip())
            except Unauthorized as exc:
                return err(401, exc.code, exc.message)
            except AppError as exc:
                return err(exc.status, exc.code, exc.message)
            if claims.get("typ", "access") != "access":
                return err(401, "invalid_token", "not an access token")
            principal_key = f"user:{claims['sub']}"
            extra = {"X-User-Id": str(claims["sub"]), "X-User-Name": claims.get("username", ""),
                     "X-User-Roles": ",".join(claims.get("roles", []))}

        allowed, retry_after = limiter.allow(principal_key)
        if not allowed:
            return err(429, "rate_limited", "too many requests", headers={"Retry-After": str(int(retry_after) + 1)})

        headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP_HEADERS}
        headers["X-Request-ID"] = request_id_var.get()
        headers.update(extra)
        body = await request.body()
        try:
            resp = await request.app.state.client.request(
                request.method, f"{route.base_url}{upstream_path}", params=request.query_params, content=body,
                headers=headers, timeout=route.timeout)
        except httpx.TimeoutException:
            log_event(log, "upstream timeout", logging.WARNING, upstream=route.service, path=upstream_path)
            return err(504, "upstream_timeout", f"{route.service} did not answer within {route.timeout}s")
        except httpx.HTTPError as exc:
            log_event(log, "upstream unavailable", logging.WARNING, upstream=route.service, error=type(exc).__name__)
            return err(503, "upstream_unavailable", f"{route.service} is unavailable")
        out_headers = {k: v for k, v in resp.headers.items()
                       if k.lower() not in ("content-length", "transfer-encoding", "connection", "content-encoding", "x-request-id")}
        return Response(content=resp.content, status_code=resp.status_code, headers=out_headers)

    return app


app = build_app()
