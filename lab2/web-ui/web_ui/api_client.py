"""Клиент HTTP к API Gateway: самый нижний слой web-ui.

Берёт: метод, путь /api/..., тело запроса и токен пользователя.
Делает: отправляет запрос в gateway с заголовками Authorization и X-Request-ID;
  ответ с ошибкой (код 4xx/5xx) превращает в исключение ApiError с кодом из общего формата ошибок.
Получается: данные из JSON-ответа или ApiError.
Зачем: репозитории (repositories.py) не думают про HTTP, заголовки и разбор ошибок.
"""
from __future__ import annotations

from typing import Any

import httpx

from pad_common.logging import request_id_var


class ApiError(Exception):
    """Ошибка от gateway или сервиса в общем формате {"error": {"code", "message", "details"}}."""

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


class GatewayClient:
    def __init__(self, http: httpx.Client, base_url: str, token: str | None = None):
        self.http, self.base_url, self.token = http, base_url.rstrip("/"), token

    def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None) -> Any:
        headers = {"X-Request-ID": request_id_var.get()}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if params:
            params = {k: v for k, v in params.items() if v not in (None, "")}
        try:
            resp = self.http.request(method, self.base_url + path, json=json, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise ApiError(504, "upstream_timeout", "gateway did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ApiError(503, "upstream_unavailable", "gateway is unavailable") from exc
        if resp.status_code >= 400:
            try:
                err = resp.json().get("error", {})
            except ValueError:
                err = {}
            raise ApiError(resp.status_code, err.get("code", "error"), err.get("message", resp.text[:200]),
                           err.get("details"))
        return resp.json() if resp.content else None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params)

    def post(self, path: str, json: Any = None) -> Any:
        return self.request("POST", path, json=json if json is not None else {})

    def put(self, path: str, json: Any) -> Any:
        return self.request("PUT", path, json=json)
