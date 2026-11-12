"""fulfillment-service: сборка заказов (адресная, по ячейкам), волны и отгрузки.

Слушает order.confirmed -> создаёт задание на сборку с маршрутом по ячейкам из резерва;
        order.cancelled -> отменяет несобранное задание.
GET  /pickings, GET /pickings/{id}
POST /pickings/{id}/start                    сборщик берёт задание
POST /pickings/{id}/lines/{line_id}/pick     сборщик сканирует ячейку и указывает количество
POST /pickings/{id}/complete                 конец сборки -> событие picking.completed + отгрузка
POST /waves, GET /waves/{id}                 волна: несколько заданий и сводный маршрут
GET  /shipments, POST /shipments/{id}/dispatch -> событие shipment.dispatched
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from pad_common import contracts as c
from pad_common.app import create_service, get_session
from pad_common.auth import Principal, require_roles
from pad_common.config import BaseServiceSettings
from pad_common.errors import NotFound

from . import logic
from .models import Picking, Shipment, Wave


class Settings(BaseServiceSettings):
    service_name: str = "fulfillment-service"


class PickIn(BaseModel):
    location_code: str = Field(..., min_length=1, max_length=32, description="код отсканированной ячейки")
    quantity: int = Field(..., gt=0)


class WaveIn(BaseModel):
    max_pickings: int = Field(10, ge=1, le=100)
    picking_ids: list[int] | None = None


class DispatchIn(BaseModel):
    carrier: str = Field(..., min_length=1, max_length=64, examples=["Nova Poshta"])
    tracking_number: str = Field(..., min_length=1, max_length=64, examples=["TRK-000123"])


def picking_view(p: Picking) -> dict:
    return {"id": p.id, "order_id": p.order_id, "order_number": p.order_number, "customer_name": p.customer_name,
            "status": p.status, "assigned_to": p.assigned_to, "wave_id": p.wave_id, "created_at": p.created_at,
            "completed_at": p.completed_at,
            "lines": [{"id": l.id, "sequence": l.sequence, "location_code": l.location_code, "product_id": l.product_id,
                       "quantity": l.quantity, "picked_quantity": l.picked_quantity} for l in p.lines]}


def shipment_view(sh: Shipment) -> dict:
    return {"id": sh.id, "order_id": sh.order_id, "order_number": sh.order_number, "picking_id": sh.picking_id,
            "status": sh.status, "carrier": sh.carrier, "tracking_number": sh.tracking_number,
            "created_at": sh.created_at, "dispatched_at": sh.dispatched_at}


def build_app(settings: Settings | None = None) -> FastAPI:
    app = create_service(settings or Settings(), title="fulfillment-service",
                         description="Сборка заказов по ячейкам, волновая сборка и отгрузки.")
    pickers = require_roles("picker")
    managers = require_roles("manager")
    anyone = require_roles()

    def on_order_confirmed(s: Session, env) -> None:
        logic.create_from_order(s, c.OrderConfirmed.model_validate(env.payload))

    def on_order_cancelled(s: Session, env) -> None:
        logic.cancel_for_order(s, c.OrderCancelled.model_validate(env.payload).order_id)

    app.state.bus.subscribe(c.ORDER_CONFIRMED, on_order_confirmed)
    app.state.bus.subscribe(c.ORDER_CANCELLED, on_order_cancelled)
    app.state.handlers = {c.ORDER_CONFIRMED: on_order_confirmed, c.ORDER_CANCELLED: on_order_cancelled}

    @app.get("/pickings", tags=["pickings"])
    def list_pickings(status: str | None = None, wave_id: int | None = None, limit: int = Query(50, ge=1, le=500),
                      _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        q = select(Picking).order_by(Picking.id)
        if status:
            q = q.where(Picking.status == status)
        if wave_id is not None:
            q = q.where(Picking.wave_id == wave_id)
        return [picking_view(p) for p in s.scalars(q.limit(limit))]

    @app.get("/pickings/{picking_id}", tags=["pickings"])
    def get_picking(picking_id: int, _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        return picking_view(logic.get_picking(s, picking_id))

    @app.post("/pickings/{picking_id}/start", tags=["pickings"])
    def start(picking_id: int, p: Principal = Depends(pickers), s: Session = Depends(get_session)):
        return picking_view(logic.start(s, picking_id, p))

    @app.post("/pickings/{picking_id}/lines/{line_id}/pick", tags=["pickings"])
    def pick(picking_id: int, line_id: int, body: PickIn, p: Principal = Depends(pickers),
             s: Session = Depends(get_session)):
        logic.pick_line(s, picking_id, line_id, body.location_code, body.quantity, p)
        return picking_view(logic.get_picking(s, picking_id))

    @app.post("/pickings/{picking_id}/complete", tags=["pickings"])
    def complete(picking_id: int, p: Principal = Depends(pickers), s: Session = Depends(get_session)):
        return picking_view(logic.complete(s, picking_id, p))

    @app.post("/waves", status_code=201, tags=["waves"], summary="Собрать готовые задания в волну")
    def create_wave(body: WaveIn, p: Principal = Depends(managers), s: Session = Depends(get_session)):
        w = logic.create_wave(s, body.max_pickings, body.picking_ids, p)
        return {"id": w.id, "status": w.status, "pickings": [x.id for x in w.pickings], "route": logic.wave_route(w)}

    @app.get("/waves/{wave_id}", tags=["waves"], summary="Волна и сводный маршрут по ячейкам")
    def get_wave(wave_id: int, _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        w = s.get(Wave, wave_id)
        if w is None:
            raise NotFound(f"wave {wave_id} not found")
        return {"id": w.id, "status": w.status, "created_by": w.created_by, "pickings": [x.id for x in w.pickings],
                "route": logic.wave_route(w)}

    @app.get("/shipments", tags=["shipments"])
    def list_shipments(status: str | None = None, _: Principal = Depends(anyone), s: Session = Depends(get_session)):
        q = select(Shipment).order_by(Shipment.id)
        if status:
            q = q.where(Shipment.status == status)
        return [shipment_view(x) for x in s.scalars(q)]

    @app.post("/shipments/{shipment_id}/dispatch", tags=["shipments"])
    def dispatch(shipment_id: int, body: DispatchIn, _: Principal = Depends(managers), s: Session = Depends(get_session)):
        return shipment_view(logic.dispatch(s, shipment_id, body.carrier, body.tracking_number))

    return app


app = build_app()
