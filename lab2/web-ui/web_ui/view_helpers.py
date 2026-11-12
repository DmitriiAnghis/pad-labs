"""Помощники для шаблонов (View): русские подписи статусов и кодов ошибок, формат дат.

Берёт: значения из API (status="in_progress", code="insufficient_stock", дата в ISO).
Делает: переводит их в понятный текст для страницы.
Получается: фильтры Jinja: {{ o.status|status }}, {{ o.created_at|dt }}.
"""
from __future__ import annotations

from datetime import datetime

STATUS = {
    "confirmed": "подтверждён", "picked": "собран", "shipped": "отгружен", "cancelled": "отменён",
    "ready": "ожидает", "in_progress": "в работе", "done": "готово", "dispatched": "отправлено",
}

ERRORS = {
    "unauthorized": "Нужно войти в систему",
    "token_expired": "Сессия истекла, войдите заново",
    "invalid_token": "Сессия недействительна, войдите заново",
    "invalid_credentials": "Неверный логин или пароль",
    "forbidden": "Для этого действия у вас нет нужной роли",
    "not_found": "Не найдено",
    "insufficient_stock": "Не хватает свободного товара",
    "invalid_status": "Действие невозможно в текущем статусе",
    "wrong_location": "Отсканирована не та ячейка",
    "over_pick": "Количество больше, чем нужно собрать",
    "invalid_request": "Неверные данные в форме",
    "rate_limited": "Слишком много запросов, подождите минуту",
    "upstream_unavailable": "Один из сервисов сейчас недоступен",
    "upstream_timeout": "Сервис не ответил вовремя",
}


def error_text(code: str, message: str) -> str:
    """Русский текст ошибки и исходное сообщение сервиса в скобках (оно нужно для разбора)."""
    if code.startswith("duplicate"):
        return f"Такая запись уже есть ({message})"
    ru = ERRORS.get(code)
    return f"{ru} ({message})" if ru else message


def details_text(details) -> str:
    """Подробности ошибки: для нехватки товара «товар 5: нужно 99, свободно 3», иначе как есть."""
    if isinstance(details, list) and details and all(isinstance(d, dict) and "requested" in d for d in details):
        return "; ".join(f'товар {d.get("product_id")}: нужно {d["requested"]}, свободно {d.get("available")}'
                         for d in details)
    return str(details)


def fmt_dt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).strftime("%d.%m %H:%M")
    except ValueError:
        return value


def register_filters(env) -> None:
    env.filters["status"] = lambda s: STATUS.get(s, s)
    env.filters["dt"] = fmt_dt
