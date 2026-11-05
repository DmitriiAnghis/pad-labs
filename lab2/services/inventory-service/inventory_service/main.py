"""inventory-service: ячейки, остатки, резервы под заказы, журнал движений.

Синхронно (REST):
  GET  /locations, POST /locations                    ячейки
  POST /stock/receipts                                приёмка товара в ячейку (проверяет товар в catalog-service)
  POST /stock/moves                                   перемещение между ячейками
  GET  /stock, GET /stock/summary/{product_id}        остатки
  PUT  /stock/min-levels/{product_id}                 минимальный остаток для уведомления stock.low
  GET  /movements                                     журнал движений
  POST /reservations                                  резерв под заказ (вызывает order-service)
  GET  /reservations/{order_id}, DELETE /reservations/{order_id}
Асинхронно (RabbitMQ):
  слушает picking.completed -> списать собранное; order.cancelled -> снять резерв
  публикует stock.low
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from pad_common import contracts as c
from pad_common.app import create_service, get_session
from pad_common.auth import Principal, require_roles
from pad_common.config import BaseServiceSettings
from pad_common.errors import Conflict, Unprocessable
from pad_common.http import ServiceClient
from pad_common.logging import log_event

from . import logic
from .models import Location, MinLevel, Movement, Reservation, StockItem

log = logging.getLogger("inventory")
CODE = r"^[A-Z0-9][A-Z0-9-]{0,31}$"


class Settings(BaseServiceSettings):
    service_name: str = "inventory-service"
    catalog_url: str = "http://catalog-service:8000"


class LocationIn(BaseModel):
    code: str = Field(..., pattern=CODE, examples=["A-01-02-03"])
    zone: str = Field("", max_length=16, examples=["A"])
    description: str = Field("", max_length=200)


class LocationOut(LocationIn):
    id: int


class ReceiptIn(BaseModel):
    product_id: int = Field(..., gt=0)
    location_code: str = Field(..., pattern=CODE)
    quantity: int = Field(..., gt=0, le=1_000_000)
    reference: str = Field("", max_length=64, examples=["PO-2026-0042"])


class MoveIn(BaseModel):
    product_id: int = Field(..., gt=0)
    from_location_code: str = Field(..., pattern=CODE)
    to_location_code: str = Field(..., pattern=CODE)
    quantity: int = Field(..., gt=0)


class StockOut(BaseModel):
    product_id: int
    location_code: str
    quantity: int
    reserved: int
    available: int


class MinLevelIn(BaseModel):
    min_quantity: int = Field(..., ge=0)


class ReserveLine(BaseModel):
    product_id: int = Field(..., gt=0)
    quantity: int = Field(..., gt=0, le=100_000)


class ReserveIn(BaseModel):
    order_id: str = Field(..., min_length=1, max_length=36)
    lines: list[ReserveLine] = Field(..., min_length=1, max_length=200)


def stock_out(i: StockItem) -> StockOut:
    return StockOut(product_id=i.product_id, location_code=i.location.code, quantity=i.quantity,
                    reserved=i.reserved, available=i.available)


def build_app(settings: Settings | None = None, catalog_transport=None) -> FastAPI:
    settings = settings or Settings()
    app = create_service(settings, title="inventory-service",
                         description="Ячейки, остатки, резервы под заказы и журнал движений товара.")
    catalog = ServiceClient("catalog-service", settings.catalog_url, settings.http_timeout, transport=catalog_transport)
    managers = require_roles("manager")
    staff = require_roles("manager", "picker")
    anyone = require_roles()

    # --- события ---
    def on_picking_completed(s: Session, env) -> None:
        logic.consume_picking(s, c.PickingCompleted.model_validate(env.payload))

    def on_order_cancelled(s: Session, env) -> None:
        ev = c.OrderCancelled.model_validate(env.payload)
        logic.release(s, ev.order_id, reason=f"order cancelled: {ev.reason}")

    app.state.bus.subscribe(c.PICKING_COMPLETED, on_picking_completed)
    app.state.bus.subscribe(c.ORDER_CANCELLED, on_order_cancelled)
    app.state.handlers = {c.PICKING_COMPLETED: on_picking_completed, c.ORDER_CANCELLED: on_order_cancelled}

    # --- ячейки ---
    @app.get("/locations", response_model=list[LocationOut], tags=["locations"])
    def list_locations(zone: str | None = None, _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        q = select(Location).order_by(Location.code)
        if zone:
            q = q.where(Location.zone == zone)
        return [LocationOut(id=l.id, code=l.code, zone=l.zone, description=l.description) for l in s.scalars(q)]

    @app.post("/locations", response_model=LocationOut, status_code=201, tags=["locations"])
    def create_location(body: LocationIn, _: Principal = Depends(managers), s: Session = Depends(get_session)):
        if s.scalar(select(Location).where(Location.code == body.code)):
            raise Conflict(f"location {body.code} already exists", code="duplicate_location")
        loc = Location(**body.model_dump())
        s.add(loc)
        s.flush()
        return LocationOut(id=loc.id, **body.model_dump())

    # --- остатки ---
    @app.post("/stock/receipts", response_model=StockOut, status_code=201, tags=["stock"],
              summary="Приёмка: положить товар в ячейку")
    def receipt(body: ReceiptIn, p: Principal = Depends(managers), s: Session = Depends(get_session)):
        product = catalog.get(f"/products/{body.product_id}", token=p.token)  # 404 из catalog уйдёт клиенту как 404
        if not product.get("active", True):
            raise Unprocessable(f"product {body.product_id} is deactivated", code="product_inactive")
        item = logic.receive(s, body.product_id, body.location_code, body.quantity, body.reference, p.username)
        log_event(log, "stock received", product_id=body.product_id, location=body.location_code, quantity=body.quantity)
        return stock_out(item)

    @app.post("/stock/moves", status_code=204, tags=["stock"], summary="Переместить товар между ячейками")
    def move(body: MoveIn, p: Principal = Depends(staff), s: Session = Depends(get_session)):
        logic.move(s, body.product_id, body.from_location_code, body.to_location_code, body.quantity, p.username)

    @app.get("/stock", response_model=list[StockOut], tags=["stock"])
    def list_stock(product_id: int | None = None, location_code: str | None = None, only_available: bool = False,
                   _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        q = select(StockItem).join(Location).order_by(StockItem.product_id, Location.code)
        if product_id is not None:
            q = q.where(StockItem.product_id == product_id)
        if location_code:
            q = q.where(Location.code == location_code)
        items = [stock_out(i) for i in s.scalars(q).unique()]
        return [i for i in items if i.available > 0] if only_available else items

    @app.get("/stock/summary/{product_id}", tags=["stock"])
    def summary(product_id: int, _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        rows = list(s.scalars(select(StockItem).where(StockItem.product_id == product_id)).unique())
        level = s.get(MinLevel, product_id)
        qty, res = sum(r.quantity for r in rows), sum(r.reserved for r in rows)
        return {"product_id": product_id, "quantity": qty, "reserved": res, "available": qty - res,
                "locations": len([r for r in rows if r.quantity]), "min_quantity": level.min_quantity if level else 0}

    @app.put("/stock/min-levels/{product_id}", tags=["stock"])
    def set_min(product_id: int, body: MinLevelIn, _: Principal = Depends(managers), s: Session = Depends(get_session)):
        level = s.get(MinLevel, product_id) or MinLevel(product_id=product_id)
        level.min_quantity = body.min_quantity
        s.add(level)
        return {"product_id": product_id, "min_quantity": body.min_quantity}

    @app.get("/movements", tags=["stock"])
    def movements(product_id: int | None = None, limit: int = Query(100, ge=1, le=1000),
                  _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        q = select(Movement).order_by(Movement.id.desc()).limit(limit)
        if product_id is not None:
            q = q.where(Movement.product_id == product_id)
        return [{"id": m.id, "product_id": m.product_id, "from": m.from_location, "to": m.to_location,
                 "quantity": m.quantity, "kind": m.kind, "reference": m.reference, "by": m.created_by,
                 "at": m.created_at} for m in s.scalars(q)]

    # --- резервы ---
    @app.post("/reservations", status_code=201, tags=["reservations"],
              summary="Зарезервировать товар под заказ: всё или ничего (409)")
    def reserve(body: ReserveIn, _: Principal = Depends(managers), s: Session = Depends(get_session)):
        return logic.reserve(s, body.order_id, [(l.product_id, l.quantity) for l in body.lines])

    @app.get("/reservations/{order_id}", tags=["reservations"])
    def get_reservation(order_id: str, _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        rows = list(s.scalars(select(Reservation).where(Reservation.order_id == order_id)))
        if not rows:
            from pad_common.errors import NotFound
            raise NotFound(f"no reservation for order {order_id}")
        return logic.reservation_view(order_id, rows)

    @app.delete("/reservations/{order_id}", tags=["reservations"], summary="Снять активный резерв (идемпотентно)")
    def delete_reservation(order_id: str, _: Principal = Depends(managers), s: Session = Depends(get_session)):
        return {"order_id": order_id, "released_rows": logic.release(s, order_id, reason="api")}

    return app


app = build_app()
