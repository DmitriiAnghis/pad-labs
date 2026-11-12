"""Слой Repository web-ui: доступ к данным, без логики интерфейса.

Берёт: GatewayClient (api_client.py).
Делает: у каждой сущности свой класс, который знает, по каким адресам API её читать и
  менять. Например, ProductRepository.list() = GET /api/products.
Получается: обычные словари и списки, которые дальше берут Services и Controllers.
Зачем: контроллеры не знают адресов API. Если адрес поменяется, правится одна строка здесь.
  В тестах репозиторий легко подменить.

У web-ui своей базы нет, поэтому источник данных для его репозиториев — API Gateway.
В бизнес-сервисах репозитории читают и пишут в PostgreSQL
(например, services/order-service/order_service/repository.py).
"""
from __future__ import annotations

from typing import Any

from .api_client import GatewayClient


class _Repo:
    def __init__(self, api: GatewayClient):
        self.api = api


class AuthRepository(_Repo):
    def login(self, username: str, password: str) -> dict:
        return self.api.post("/api/auth/login", {"username": username, "password": password})

    def logout(self, refresh_token: str) -> None:
        self.api.post("/api/auth/logout", {"refresh_token": refresh_token})

    def me(self) -> dict:
        return self.api.get("/api/auth/me")


class ProductRepository(_Repo):
    def list(self, q: str | None = None) -> list[dict]:
        return self.api.get("/api/products", q=q, limit=200)

    def create(self, sku: str, name: str, barcode: str | None, uom: str) -> dict:
        return self.api.post("/api/products", {"sku": sku, "name": name, "barcode": barcode or None,
                                               "uom": uom or "pcs"})


class LocationRepository(_Repo):
    def list(self) -> list[dict]:
        return self.api.get("/api/locations")

    def create(self, code: str, zone: str) -> dict:
        return self.api.post("/api/locations", {"code": code, "zone": zone})


class StockRepository(_Repo):
    def list(self, product_id: int | None = None) -> list[dict]:
        return self.api.get("/api/stock", product_id=product_id)

    def receive(self, product_id: int, location_code: str, quantity: int) -> dict:
        return self.api.post("/api/stock/receipts",
                             {"product_id": product_id, "location_code": location_code, "quantity": quantity})

    def move(self, product_id: int, from_code: str, to_code: str, quantity: int) -> Any:
        return self.api.post("/api/stock/moves", {"product_id": product_id, "from_location_code": from_code,
                                                  "to_location_code": to_code, "quantity": quantity})


class OrderRepository(_Repo):
    def list(self, status: str | None = None) -> list[dict]:
        return self.api.get("/api/orders", status=status, limit=100)

    def get(self, order_id: str) -> dict:
        return self.api.get(f"/api/orders/{order_id}")

    def create(self, customer_name: str, shipping_address: str, lines: list[dict]) -> dict:
        return self.api.post("/api/orders", {"customer_name": customer_name, "shipping_address": shipping_address,
                                             "lines": lines})

    def cancel(self, order_id: str, reason: str) -> dict:
        return self.api.post(f"/api/orders/{order_id}/cancel", {"reason": reason})


class PickingRepository(_Repo):
    def list(self, status: str | None = None) -> list[dict]:
        return self.api.get("/api/pickings", status=status, limit=100)

    def get(self, picking_id: int) -> dict:
        return self.api.get(f"/api/pickings/{picking_id}")

    def start(self, picking_id: int) -> dict:
        return self.api.post(f"/api/pickings/{picking_id}/start")

    def pick(self, picking_id: int, line_id: int, location_code: str, quantity: int) -> dict:
        return self.api.post(f"/api/pickings/{picking_id}/lines/{line_id}/pick",
                             {"location_code": location_code, "quantity": quantity})

    def complete(self, picking_id: int) -> dict:
        return self.api.post(f"/api/pickings/{picking_id}/complete")


class ShipmentRepository(_Repo):
    def list(self, status: str | None = None) -> list[dict]:
        return self.api.get("/api/shipments", status=status)

    def dispatch(self, shipment_id: int, carrier: str, tracking_number: str) -> dict:
        return self.api.post(f"/api/shipments/{shipment_id}/dispatch",
                             {"carrier": carrier, "tracking_number": tracking_number})


class NotificationRepository(_Repo):
    def list(self, unread_only: bool = False) -> list[dict]:
        return self.api.get("/api/notifications", unread_only=str(unread_only).lower(), limit=100)

    def mark_read(self, notification_id: int) -> Any:
        return self.api.post(f"/api/notifications/{notification_id}/read")
