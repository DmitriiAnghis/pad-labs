"""Логика сборки: создание задания из заказа, отметка строк, завершение, волны, отгрузка."""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from pad_common import contracts as c
from pad_common.auth import Principal
from pad_common.errors import Conflict, Forbidden, NotFound, Unprocessable
from pad_common.events import add_outbox, utcnow
from pad_common.logging import log_event

from .models import Picking, PickingLine, Shipment, Wave

log = logging.getLogger("fulfillment")
SERVICE = "fulfillment-service"


def location_key(code: str) -> tuple:
    """Порядок обхода: A-01-02-03 раньше A-01-10-01 (числа сравниваются как числа, а не как строки)."""
    return tuple(int(p) if p.isdigit() else p for p in code.split("-"))


def create_from_order(s: Session, ev: c.OrderConfirmed) -> Picking | None:
    if s.scalar(select(Picking).where(Picking.order_id == ev.order_id)):
        return None
    stops = [(a.location_code, line.product_id, a.quantity) for line in ev.lines for a in line.allocations]
    stops.sort(key=lambda t: (location_key(t[0]), t[1]))
    picking = Picking(order_id=ev.order_id, order_number=ev.order_number, customer_name=ev.customer_name)
    picking.lines = [PickingLine(sequence=i + 1, product_id=pid, location_code=loc, quantity=q)
                     for i, (loc, pid, q) in enumerate(stops)]
    s.add(picking)
    s.flush()
    log_event(log, "picking created", picking_id=picking.id, order=ev.order_number, stops=len(stops))
    return picking


def cancel_for_order(s: Session, order_id: str) -> None:
    p = s.scalar(select(Picking).where(Picking.order_id == order_id))
    if p is None:
        return
    if p.status == "done":
        log_event(log, "order cancelled after picking was completed: manual return needed", logging.WARNING,
                  picking_id=p.id)
        return
    p.status = "cancelled"
    log_event(log, "picking cancelled", picking_id=p.id)


def get_picking(s: Session, picking_id: int) -> Picking:
    p = s.get(Picking, picking_id)
    if p is None:
        raise NotFound(f"picking {picking_id} not found")
    return p


def _own(p: Picking, user: Principal) -> None:
    if p.status != "in_progress":
        raise Conflict(f"picking is '{p.status}', start it first", code="invalid_status")
    if p.assigned_to != user.username and "admin" not in user.roles:
        raise Forbidden(f"picking is assigned to {p.assigned_to}")


def start(s: Session, picking_id: int, user: Principal) -> Picking:
    p = get_picking(s, picking_id)
    if p.status == "in_progress" and p.assigned_to == user.username:
        return p
    if p.status != "ready":
        raise Conflict(f"picking is '{p.status}' and cannot be started", code="invalid_status")
    p.status, p.assigned_to = "in_progress", user.username
    return p


def pick_line(s: Session, picking_id: int, line_id: int, scanned_location: str, quantity: int, user: Principal) -> PickingLine:
    p = get_picking(s, picking_id)
    _own(p, user)
    line = next((l for l in p.lines if l.id == line_id), None)
    if line is None:
        raise NotFound(f"line {line_id} not in picking {picking_id}")
    if scanned_location != line.location_code:  # сборщик отсканировал не ту ячейку
        raise Unprocessable(f"wrong location: expected {line.location_code}, scanned {scanned_location}",
                            code="wrong_location")
    if line.picked_quantity + quantity > line.quantity:
        raise Unprocessable(f"only {line.quantity - line.picked_quantity} left to pick on this line", code="over_pick")
    line.picked_quantity += quantity
    return line


def complete(s: Session, picking_id: int, user: Principal) -> Picking:
    p = get_picking(s, picking_id)
    _own(p, user)
    short = any(l.picked_quantity < l.quantity for l in p.lines)
    p.status, p.completed_at = "done", utcnow()
    s.add(Shipment(order_id=p.order_id, order_number=p.order_number, picking_id=p.id))
    add_outbox(s, SERVICE, c.PICKING_COMPLETED, c.PickingCompleted(
        order_id=p.order_id, order_number=p.order_number, picking_id=p.id, picked_by=user.username, short=short,
        lines=[c.PickedLine(product_id=l.product_id, location_code=l.location_code, reserved_quantity=l.quantity,
                            picked_quantity=l.picked_quantity) for l in p.lines]).model_dump())
    if p.wave is not None and all(x.status in ("done", "cancelled") for x in p.wave.pickings):
        p.wave.status = "done"
    log_event(log, "picking completed", picking_id=p.id, order=p.order_number, short=short)
    return p


def create_wave(s: Session, max_pickings: int, picking_ids: list[int] | None, user: Principal) -> Wave:
    q = select(Picking).where(Picking.status == "ready", Picking.wave_id.is_(None)).order_by(Picking.created_at, Picking.id)
    if picking_ids:
        q = q.where(Picking.id.in_(picking_ids))
    pickings = list(s.scalars(q.limit(max_pickings)))
    if not pickings:
        raise Conflict("no ready pickings without a wave", code="nothing_to_wave")
    wave = Wave(created_by=user.username)
    wave.pickings = pickings
    s.add(wave)
    s.flush()
    log_event(log, "wave created", wave_id=wave.id, pickings=[p.id for p in pickings])
    return wave


def wave_route(wave: Wave) -> list[dict]:
    """Сводный маршрут волны: каждая ячейка один раз, внутри неё товары и сколько взять для каких заказов."""
    stops: dict[tuple[str, int], dict] = {}
    for p in wave.pickings:
        if p.status == "cancelled":
            continue
        for l in p.lines:
            key = (l.location_code, l.product_id)
            stop = stops.setdefault(key, {"location_code": l.location_code, "product_id": l.product_id,
                                          "quantity": 0, "orders": defaultdict(int)})
            stop["quantity"] += l.quantity
            stop["orders"][p.order_number] += l.quantity
    ordered = sorted(stops.values(), key=lambda x: (location_key(x["location_code"]), x["product_id"]))
    return [{**st, "orders": dict(st["orders"])} for st in ordered]


def dispatch(s: Session, shipment_id: int, carrier: str, tracking: str) -> Shipment:
    sh = s.get(Shipment, shipment_id)
    if sh is None:
        raise NotFound(f"shipment {shipment_id} not found")
    if sh.status != "ready":
        raise Conflict(f"shipment is already '{sh.status}'", code="invalid_status")
    sh.status, sh.carrier, sh.tracking_number, sh.dispatched_at = "dispatched", carrier, tracking, utcnow()
    add_outbox(s, SERVICE, c.SHIPMENT_DISPATCHED, c.ShipmentDispatched(
        order_id=sh.order_id, order_number=sh.order_number, shipment_id=sh.id, carrier=carrier,
        tracking_number=tracking).model_dump())
    return sh
