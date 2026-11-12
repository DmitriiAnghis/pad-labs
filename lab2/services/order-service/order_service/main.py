"""order-service: заказы и их статусы.

POST /orders              создать заказ: синхронно резервирует товар в inventory-service,
                          при успехе сохраняет заказ и событие order.confirmed (outbox)
GET  /orders              список (фильтр по статусу)
GET  /orders/{id}         заказ со строками, ячейками резерва и историей статусов
POST /orders/{id}/cancel  отменить (только confirmed) -> событие order.cancelled
Слушает picking.completed -> picked, shipment.dispatched -> shipped.

Слои: обработчики ниже (Controller) -> OrderRepository (repository.py, запросы к базе)
  -> Order (models.py); правила статусов в domain.py (Services/доменная логика).
Репозиторий обработчик получает через DI: repo: OrderRepository = Depends(order_repo).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from pad_common import contracts as c
from pad_common.app import create_service, get_session
from pad_common.auth import Principal, require_roles
from pad_common.config import BaseServiceSettings
from pad_common.errors import AppError, Conflict, UpstreamError
from pad_common.events import add_outbox
from pad_common.http import ServiceClient
from pad_common.logging import log_event

from . import domain
from .models import Order, OrderLine, StatusChange
from .repository import OrderRepository

log = logging.getLogger("orders")
SERVICE = "order-service"


class Settings(BaseServiceSettings):
    service_name: str = "order-service"
    inventory_url: str = "http://inventory-service:8000"


class LineIn(BaseModel):
    product_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=100_000)


class OrderIn(BaseModel):
    customer_name: str = Field(..., min_length=1, max_length=200, examples=["ООО Ромашка"])
    shipping_address: str = Field(..., min_length=1, max_length=500, examples=["Кишинёв, ул. Штефан чел Маре 1"])
    lines: list[LineIn] = Field(..., min_length=1, max_length=200)


class CancelIn(BaseModel):
    reason: str = Field("", max_length=200)


def order_view(o: Order, full: bool = True) -> dict:
    data = {"id": o.id, "number": o.number, "status": o.status, "customer_name": o.customer_name,
            "shipping_address": o.shipping_address, "created_by": o.created_by, "short_picked": o.short_picked,
            "tracking_number": o.tracking_number, "created_at": o.created_at, "updated_at": o.updated_at}
    if full:
        data["lines"] = [{"product_id": l.product_id, "quantity": l.quantity, "allocations": l.allocations} for l in o.lines]
        data["history"] = [{"status": h.status, "note": h.note, "at": h.at} for h in o.history]
    return data


def set_status(o: Order, new: str, note: str = "") -> bool:
    if not domain.can_move(o.status, new):
        log_event(log, "status change ignored", logging.WARNING, order_id=o.id, current=o.status, requested=new)
        return False
    o.status = new
    o.history.append(StatusChange(order_id=o.id, status=new, note=note))
    return True


def order_repo(s: Session = Depends(get_session)) -> OrderRepository:
    """DI: FastAPI создаёт репозиторий на каждый запрос из сессии этого запроса."""
    return OrderRepository(s)


def build_app(settings: Settings | None = None, inventory_transport=None) -> FastAPI:
    settings = settings or Settings()
    app = create_service(settings, title="order-service",
                         description="Заказы: создание с резервом товара, статусы, отмена.")
    inventory = ServiceClient("inventory-service", settings.inventory_url, settings.http_timeout,
                              transport=inventory_transport)
    managers = require_roles("manager")
    anyone = require_roles()

    def compensate(order_id: str, token: str) -> None:
        """Резерв мог успеть создаться, хотя ответ не пришёл. Снимаем его, чтобы товар не завис."""
        try:
            inventory.delete(f"/reservations/{order_id}", token=token)
        except AppError as exc:
            log_event(log, "compensation failed, reservation may stay until manual release", logging.ERROR,
                      order_id=order_id, error=exc.message)

    @app.post("/orders", status_code=201, tags=["orders"], summary="Создать заказ (резерв товара синхронно)")
    def create_order(body: OrderIn, p: Principal = Depends(managers), s: Session = Depends(get_session)):
        order_id = uuid.uuid4().hex
        number = f"ORD-{datetime.now(timezone.utc):%Y%m%d}-{order_id[:6].upper()}"
        try:
            res = inventory.post("/reservations", token=p.token, json={
                "order_id": order_id, "lines": [l.model_dump() for l in body.lines]})
        except UpstreamError:
            compensate(order_id, p.token)
            raise
        alloc = {line["product_id"]: line["allocations"] for line in res["lines"]}
        try:
            order = Order(id=order_id, number=number, status=domain.CONFIRMED, customer_name=body.customer_name,
                          shipping_address=body.shipping_address, created_by=p.username)
            merged: dict[int, int] = {}
            for l in body.lines:
                merged[l.product_id] = merged.get(l.product_id, 0) + l.quantity
            order.lines = [OrderLine(product_id=pid, quantity=q, allocations=alloc.get(pid, [])) for pid, q in merged.items()]
            order.history = [StatusChange(order_id=order_id, status=domain.CONFIRMED, note="stock reserved")]
            OrderRepository(s).add(order)
            add_outbox(s, SERVICE, c.ORDER_CONFIRMED, c.OrderConfirmed(
                order_id=order_id, order_number=number, customer_name=body.customer_name,
                lines=[c.ConfirmedLine(product_id=l.product_id, quantity=l.quantity,
                                       allocations=[c.Allocation(**a) for a in l.allocations]) for l in order.lines],
            ).model_dump())
            s.flush()
        except Exception:
            compensate(order_id, p.token)
            raise
        log_event(log, "order confirmed", order_id=order_id, number=number, lines=len(order.lines))
        return order_view(order)

    @app.get("/orders", tags=["orders"])
    def list_orders(status: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                    _: Principal = Depends(anyone), repo: OrderRepository = Depends(order_repo)):
        return [order_view(o, full=False) for o in repo.list(status, limit, offset)]

    @app.get("/orders/{order_id}", tags=["orders"])
    def get_order(order_id: str, _: Principal = Depends(anyone), repo: OrderRepository = Depends(order_repo)):
        return order_view(repo.get(order_id))

    @app.post("/orders/{order_id}/cancel", tags=["orders"], summary="Отменить подтверждённый заказ")
    def cancel(order_id: str, body: CancelIn, p: Principal = Depends(managers), s: Session = Depends(get_session)):
        o = OrderRepository(s).get(order_id)
        if not set_status(o, domain.CANCELLED, body.reason):
            raise Conflict(f"order in status '{o.status}' cannot be cancelled", code="invalid_status")
        add_outbox(s, SERVICE, c.ORDER_CANCELLED,
                   c.OrderCancelled(order_id=o.id, order_number=o.number, reason=body.reason).model_dump())
        log_event(log, "order cancelled", order_id=o.id, by=p.username)
        return order_view(o)

    def on_picking_completed(s: Session, env) -> None:
        ev = c.PickingCompleted.model_validate(env.payload)
        o = OrderRepository(s).find(ev.order_id)
        if o is not None and set_status(o, domain.PICKED, f"picked by {ev.picked_by}" + (" (short)" if ev.short else "")):
            o.short_picked = ev.short

    def on_shipment_dispatched(s: Session, env) -> None:
        ev = c.ShipmentDispatched.model_validate(env.payload)
        o = OrderRepository(s).find(ev.order_id)
        if o is not None and set_status(o, domain.SHIPPED, f"{ev.carrier} {ev.tracking_number}"):
            o.tracking_number = ev.tracking_number

    app.state.bus.subscribe(c.PICKING_COMPLETED, on_picking_completed)
    app.state.bus.subscribe(c.SHIPMENT_DISPATCHED, on_shipment_dispatched)
    app.state.handlers = {c.PICKING_COMPLETED: on_picking_completed, c.SHIPMENT_DISPATCHED: on_shipment_dispatched}
    return app


app = build_app()
