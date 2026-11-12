"""Бизнес-логика остатков. Каждая функция работает внутри одной транзакции (session).

Главное правило: зарезервировать можно только свободный остаток (quantity - reserved).
Строки остатков блокируются (SELECT ... FOR UPDATE в PostgreSQL), поэтому два заказа,
пришедшие одновременно, не заберут один и тот же товар: второй ждёт, пока первый закончит,
и видит уже уменьшенный свободный остаток.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pad_common import contracts as c
from pad_common.errors import Conflict, NotFound, Unprocessable
from pad_common.events import add_outbox
from pad_common.logging import log_event

from .models import Location, MinLevel, Movement, Reservation, StockItem

log = logging.getLogger("inventory")
SERVICE = "inventory-service"


def get_location(s: Session, code: str) -> Location:
    loc = s.scalar(select(Location).where(Location.code == code))
    if loc is None:
        raise NotFound(f"location {code} not found", code="location_not_found")
    return loc


def _locked_stock(s: Session, product_id: int, location_id: int | None = None):
    q = select(StockItem).join(Location).where(StockItem.product_id == product_id)
    if location_id is not None:
        q = q.where(StockItem.location_id == location_id)
    q = q.order_by(Location.code).with_for_update(of=StockItem)
    return list(s.scalars(q).unique())


def receive(s: Session, product_id: int, location_code: str, quantity: int, reference: str, user: str) -> StockItem:
    loc = get_location(s, location_code)
    rows = _locked_stock(s, product_id, loc.id)
    item = rows[0] if rows else StockItem(product_id=product_id, location_id=loc.id, quantity=0, reserved=0)
    if not rows:
        s.add(item)
    item.quantity += quantity
    s.add(Movement(product_id=product_id, to_location=loc.code, quantity=quantity, kind="receipt",
                   reference=reference, created_by=user))
    s.flush()
    return item


def move(s: Session, product_id: int, src: str, dst: str, quantity: int, user: str) -> None:
    if src == dst:
        raise Unprocessable("source and destination are the same location")
    a, b = get_location(s, src), get_location(s, dst)
    rows = _locked_stock(s, product_id, a.id)
    if not rows or rows[0].available < quantity:
        have = rows[0].available if rows else 0
        raise Conflict(f"only {have} free units of product {product_id} in {src}", code="insufficient_stock")
    rows[0].quantity -= quantity
    target = _locked_stock(s, product_id, b.id)
    if target:
        target[0].quantity += quantity
    else:
        s.add(StockItem(product_id=product_id, location_id=b.id, quantity=quantity, reserved=0))
    s.add(Movement(product_id=product_id, from_location=src, to_location=dst, quantity=quantity, kind="move", created_by=user))


def reservation_view(order_id: str, rows: list[Reservation]) -> dict:
    by_product: dict[int, list[Reservation]] = defaultdict(list)
    for r in rows:
        by_product[r.product_id].append(r)
    return {
        "order_id": order_id,
        "status": rows[0].status if rows else "none",
        "lines": [{"product_id": pid, "quantity": sum(r.quantity for r in rs),
                   "allocations": [{"location_code": r.location.code, "quantity": r.quantity} for r in rs]}
                  for pid, rs in by_product.items()],
    }


def reserve(s: Session, order_id: str, lines: list[tuple[int, int]]) -> dict:
    """Зарезервировать все строки заказа целиком или ничего (409 и откат транзакции).

    Повторный вызов с тем же order_id возвращает уже сделанный резерв (идемпотентность):
    order-service может повторить запрос после таймаута и не получит двойной резерв.
    """
    existing = list(s.scalars(select(Reservation).where(Reservation.order_id == order_id, Reservation.status == "active")))
    if existing:
        return reservation_view(order_id, existing)
    wanted: dict[int, int] = defaultdict(int)
    for product_id, qty in lines:
        wanted[product_id] += qty
    shortages = []
    created: list[Reservation] = []
    for product_id in sorted(wanted):  # одинаковый порядок блокировок у всех запросов = нет взаимных блокировок
        need = wanted[product_id]
        rows = _locked_stock(s, product_id)
        free = sum(r.available for r in rows)
        if free < need:
            shortages.append({"product_id": product_id, "requested": need, "available": free})
            continue
        for item in rows:  # берём из ячеек по порядку кода (короче маршрут сборщика)
            if need == 0:
                break
            take = min(item.available, need)
            if take <= 0:
                continue
            item.reserved += take
            need -= take
            res = Reservation(order_id=order_id, product_id=product_id, location_id=item.location_id, quantity=take)
            res.location = item.location
            created.append(res)
    if shortages:
        raise Conflict("not enough stock for the order", details=shortages, code="insufficient_stock")
    s.add_all(created)
    s.flush()
    log_event(log, "stock reserved", order_id=order_id, lines=len(wanted))
    return reservation_view(order_id, created)


def release(s: Session, order_id: str, reason: str) -> int:
    rows = list(s.scalars(select(Reservation).where(Reservation.order_id == order_id, Reservation.status == "active")))
    for r in rows:
        item = _locked_stock(s, r.product_id, r.location_id)[0]
        item.reserved -= r.quantity
        r.status = "released"
    if rows:
        log_event(log, "reservation released", order_id=order_id, rows=len(rows), reason=reason)
    return len(rows)


def total_quantity(s: Session, product_id: int) -> int:
    return int(s.scalar(select(func.coalesce(func.sum(StockItem.quantity), 0)).where(StockItem.product_id == product_id)))


def check_low_stock(s: Session, product_ids: set[int]) -> None:
    for pid in sorted(product_ids):
        level = s.get(MinLevel, pid)
        if level is None or level.min_quantity <= 0:
            continue
        qty = total_quantity(s, pid)
        if qty < level.min_quantity:
            add_outbox(s, SERVICE, c.STOCK_LOW, c.StockLow(product_id=pid, quantity=qty, min_quantity=level.min_quantity).model_dump())
            log_event(log, "stock below minimum", logging.WARNING, product_id=pid, quantity=qty, min=level.min_quantity)


def consume_picking(s: Session, ev: c.PickingCompleted) -> None:
    """Сборка завершена: списать собранное, остаток резерва (если собрали меньше) освободить."""
    picked: dict[tuple[int, str], int] = defaultdict(int)
    for line in ev.lines:
        picked[(line.product_id, line.location_code)] += line.picked_quantity
    rows = list(s.scalars(select(Reservation).where(Reservation.order_id == ev.order_id, Reservation.status == "active")))
    touched: set[int] = set()
    for r in rows:
        item = _locked_stock(s, r.product_id, r.location_id)[0]
        key = (r.product_id, r.location.code)
        take = min(picked.get(key, 0), r.quantity)
        picked[key] = picked.get(key, 0) - take
        item.reserved -= r.quantity
        item.quantity -= take
        r.status = "consumed"
        touched.add(r.product_id)
        if take:
            s.add(Movement(product_id=r.product_id, from_location=r.location.code, quantity=take, kind="pick",
                           reference=ev.order_number, created_by=ev.picked_by))
    s.flush()
    log_event(log, "picking consumed", order_id=ev.order_id, reservations=len(rows), short=ev.short)
    check_low_stock(s, touched)
