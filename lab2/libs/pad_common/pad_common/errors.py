"""Единый формат ошибок всех сервисов.

Любая ошибка уходит клиенту как {"error": {"code", "message", "details"}, "request_id"}.
Stack trace наружу не отдаётся никогда, он остаётся только в логах сервиса.
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str, details: Any = None, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details
        if code:
            self.code = code


class Unauthorized(AppError):
    status, code = 401, "unauthorized"


class Forbidden(AppError):
    status, code = 403, "forbidden"


class NotFound(AppError):
    status, code = 404, "not_found"


class Conflict(AppError):
    status, code = 409, "conflict"


class Unprocessable(AppError):
    status, code = 422, "invalid_request"


class UpstreamError(AppError):
    """Другой сервис не ответил, ответил ошибкой 5xx или истёк таймаут."""
    status, code = 503, "upstream_unavailable"


class UpstreamTimeout(UpstreamError):
    status, code = 504, "upstream_timeout"


def error_body(code: str, message: str, request_id: str, details: Any = None) -> dict:
    body: dict = {"error": {"code": code, "message": message}, "request_id": request_id}
    if details is not None:
        body["error"]["details"] = details
    return body
