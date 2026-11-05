"""Таблица маршрутов: какой префикс внешнего адреса уходит в какой сервис.

Внешний адрес              -> сервис              -> адрес внутри сервиса
/api/auth/login            -> auth-service        -> /auth/login
/api/products/42           -> catalog-service     -> /products/42
/api/assistant/ask         -> rag-assistant       -> /ask   (префикс assistant отрезается)
"""
from __future__ import annotations

from dataclasses import dataclass

from .config import Settings


@dataclass(frozen=True)
class Route:
    prefix: str        # первая часть пути после /api/
    service: str
    base_url: str
    strip_prefix: bool = False
    timeout: float = 10.0


# Эти пути доступны без токена: иначе невозможно войти.
PUBLIC = {("POST", "auth/login"), ("POST", "auth/refresh"), ("POST", "auth/logout"), ("GET", "auth/public-key")}


def build_routes(s: Settings) -> list[Route]:
    t = s.upstream_timeout
    return [
        Route("auth", "auth-service", s.auth_url, timeout=t),
        Route("users", "auth-service", s.auth_url, timeout=t),
        Route("products", "catalog-service", s.catalog_url, timeout=t),
        Route("locations", "inventory-service", s.inventory_url, timeout=t),
        Route("stock", "inventory-service", s.inventory_url, timeout=t),
        Route("reservations", "inventory-service", s.inventory_url, timeout=t),
        Route("orders", "order-service", s.order_url, timeout=t),
        Route("pickings", "fulfillment-service", s.fulfillment_url, timeout=t),
        Route("waves", "fulfillment-service", s.fulfillment_url, timeout=t),
        Route("shipments", "fulfillment-service", s.fulfillment_url, timeout=t),
        Route("notifications", "notification-service", s.notification_url, timeout=t),
        Route("assistant", "rag-assistant", s.rag_url, strip_prefix=True, timeout=s.rag_timeout),
    ]


def match(routes: list[Route], path: str) -> tuple[Route, str] | None:
    """path без /api/, например 'products/42'. Возвращает маршрут и путь внутри сервиса."""
    head = path.split("/", 1)[0]
    for r in routes:
        if head == r.prefix:
            rest = path[len(head):].lstrip("/")
            return r, ("/" + rest if r.strip_prefix else "/" + path)
    return None
