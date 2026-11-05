"""Структурированные логи: одна строка = один JSON-объект.

Поля, которые требует лаба 2: timestamp, service, level, request_id, message.
request_id берётся из заголовка X-Request-ID (его ставит gateway), поэтому по одному id
можно найти строки всех сервисов, через которые прошёл один запрос пользователя.
Код взят из lab1/src/common/logging_setup.py и обобщён: имя сервиса теперь параметр.
"""
from __future__ import annotations

import contextvars
import json
import logging
import sys
import time

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
_service_name = "service"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
            "service": _service_name,
            "level": record.levelname,
            "request_id": request_id_var.get(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        if record.exc_info:
            payload["error"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        line = f"{time.strftime('%H:%M:%S', time.localtime(record.created))} {record.levelname:<7} {_service_name}/{record.name}: {record.getMessage()}"
        rid = request_id_var.get()
        if rid != "-":
            line += f" [req={rid}]"
        extra = getattr(record, "extra_fields", None)
        if extra:
            line += " " + " ".join(f"{k}={v}" for k, v in extra.items())
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def setup_logging(service_name: str, level: str = "INFO", fmt: str = "json") -> None:
    global _service_name
    _service_name = service_name
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn пишет свой access-лог; у нас есть свой с request_id, поэтому его выключаем.
    logging.getLogger("uvicorn.access").disabled = True
    for name in ("aio_pika", "aiormq", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, message: str, level: int = logging.INFO, **fields) -> None:
    """Записать строку лога с дополнительными полями (order_id=..., status=...)."""
    logger.log(level, message, extra={"extra_fields": fields})
