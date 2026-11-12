"""Содержимое (payload) событий: общий «договор» между сервисами.

Издатель и получатели описывают одно и то же событие одной и той же моделью, поэтому
переименование поля сразу ломает тесты, а не тихо теряет данные в работе.
Здесь только форма сообщений, никакой логики.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

ORDER_CONFIRMED = "order.confirmed"
ORDER_CANCELLED = "order.cancelled"
PICKING_COMPLETED = "picking.completed"
SHIPMENT_DISPATCHED = "shipment.dispatched"
STOCK_LOW = "stock.low"


class Allocation(BaseModel):
    """Сколько штук товара зарезервировано в конкретной ячейке."""
    location_code: str
    quantity: int = Field(..., gt=0)


class ConfirmedLine(BaseModel):
    product_id: int
    quantity: int
    allocations: list[Allocation]


class OrderConfirmed(BaseModel):
    order_id: str
    order_number: str
    customer_name: str
    lines: list[ConfirmedLine]


class OrderCancelled(BaseModel):
    order_id: str
    order_number: str
    reason: str = ""


class PickedLine(BaseModel):
    product_id: int
    location_code: str
    reserved_quantity: int
    picked_quantity: int


class PickingCompleted(BaseModel):
    order_id: str
    order_number: str
    picking_id: int
    picked_by: str
    short: bool = False  # собрано меньше, чем зарезервировано
    lines: list[PickedLine]


class ShipmentDispatched(BaseModel):
    order_id: str
    order_number: str
    shipment_id: int
    carrier: str
    tracking_number: str


class StockLow(BaseModel):
    product_id: int
    quantity: int
    min_quantity: int
