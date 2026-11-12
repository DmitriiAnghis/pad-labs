"""Синхронные запросы к другим сервисам (REST).

Берёт: адрес другого сервиса (из переменной окружения, например INVENTORY_URL), токен
пользователя и текущий request_id.
Делает: отправляет запрос с таймаутом; передаёт дальше X-Request-ID и Authorization.
Получается: JSON ответа или понятная ошибка:
  * 4xx от другого сервиса -> та же ошибка (404, 409, 422 ...), сообщение сохраняется;
  * 5xx или нет соединения -> 503 upstream_unavailable;
  * истёк таймаут -> 504 upstream_timeout.
Зачем: вызывающий сервис не зависает, если другой сервис лежит или тормозит.
"""
from __future__ import annotations

import logging
import time

import httpx

from .errors import AppError, UpstreamError, UpstreamTimeout
from .logging import log_event, request_id_var

log = logging.getLogger("http")


class ServiceClient:
    def __init__(self, name: str, base_url: str, timeout: float = 2.0, transport: httpx.BaseTransport | None = None) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.transport = transport  # в тестах сюда подставляется TestClient другого сервиса

    def request(self, method: str, path: str, token: str | None = None, **kwargs) -> dict:
        headers = {"X-Request-ID": request_id_var.get()}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        start = time.perf_counter()
        try:
            with httpx.Client(base_url=self.base_url, timeout=self.timeout, transport=self.transport) as client:
                r = client.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            log_event(log, "upstream timeout", logging.WARNING, upstream=self.name, path=path)
            raise UpstreamTimeout(f"{self.name} did not answer within {self.timeout}s") from exc
        except httpx.HTTPError as exc:
            log_event(log, "upstream unreachable", logging.WARNING, upstream=self.name, path=path, error=type(exc).__name__)
            raise UpstreamError(f"{self.name} is unavailable") from exc
        ms = round((time.perf_counter() - start) * 1000, 1)
        log_event(log, "upstream call", upstream=self.name, method=method, path=path, status=r.status_code, ms=ms)
        if r.status_code >= 500:
            raise UpstreamError(f"{self.name} failed with HTTP {r.status_code}")
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            e = AppError(err.get("message") or f"{self.name} returned HTTP {r.status_code}", err.get("details"),
                         code=err.get("code"))
            e.status = r.status_code
            raise e
        return r.json() if r.content else {}

    def get(self, path: str, token: str | None = None, **kw) -> dict:
        return self.request("GET", path, token, **kw)

    def post(self, path: str, token: str | None = None, **kw) -> dict:
        return self.request("POST", path, token, **kw)

    def delete(self, path: str, token: str | None = None, **kw) -> dict:
        return self.request("DELETE", path, token, **kw)
