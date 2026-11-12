"""notification-service: превращает события в уведомления для ролей.

Берёт: события order.confirmed, order.cancelled, picking.completed, shipment.dispatched, stock.low.
Делает: для каждого события по таблице RULES составляет текст и адресата (роль).
Получается: строки таблицы notifications; GET /notifications отдаёт их пользователю по его ролям.
Зачем: главный пример асинхронности. Если сервис выключен, события ждут в очереди RabbitMQ,
и после запуска все уведомления появятся; остальные сервисы этого даже не замечают.
Отправка почты/мессенджера сознательно не сделана: вместо неё запись в лог (см. DECISIONS.md).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from fastapi import Depends, FastAPI, Query
from sqlalchemy import DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from pad_common import contracts as c
from pad_common.app import create_service, get_session
from pad_common.auth import Principal, require_roles
from pad_common.config import BaseServiceSettings
from pad_common.db import Base
from pad_common.errors import Forbidden, NotFound
from pad_common.events import EventEnvelope, utcnow
from pad_common.logging import log_event

log = logging.getLogger("notifications")


class Settings(BaseServiceSettings):
    service_name: str = "notification-service"


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(32), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    audience: Mapped[str] = mapped_column(String(16), index=True)  # роль: manager / picker
    title: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(String(1000))
    order_number: Mapped[str] = mapped_column(String(32), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# Тип события -> функция (payload) -> список (роль, заголовок, текст, номер заказа)
Rule = Callable[[dict], list[tuple[str, str, str, str]]]


def _confirmed(p: dict) -> list:
    ev = c.OrderConfirmed.model_validate(p)
    units = sum(l.quantity for l in ev.lines)
    return [("picker", f"Новое задание на сборку {ev.order_number}",
             f"Заказ {ev.order_number} ({ev.customer_name}): {len(ev.lines)} позиций, {units} шт.", ev.order_number),
            ("manager", f"Заказ {ev.order_number} подтверждён", f"Товар зарезервирован для {ev.customer_name}.", ev.order_number)]


def _cancelled(p: dict) -> list:
    ev = c.OrderCancelled.model_validate(p)
    reason = f" Причина: {ev.reason}." if ev.reason else ""
    return [("picker", f"Заказ {ev.order_number} отменён", f"Не собирайте заказ {ev.order_number}.{reason}", ev.order_number)]


def _picked(p: dict) -> list:
    ev = c.PickingCompleted.model_validate(p)
    if ev.short:
        missing = sum(l.reserved_quantity - l.picked_quantity for l in ev.lines)
        return [("manager", f"Недобор в заказе {ev.order_number}",
                 f"{ev.picked_by} собрал заказ, не хватило {missing} шт. Проверьте ячейки.", ev.order_number)]
    return [("manager", f"Заказ {ev.order_number} собран", f"Сборщик {ev.picked_by}. Можно отгружать.", ev.order_number)]


def _dispatched(p: dict) -> list:
    ev = c.ShipmentDispatched.model_validate(p)
    return [("manager", f"Заказ {ev.order_number} отгружен", f"{ev.carrier}, трек {ev.tracking_number}.", ev.order_number)]


def _low(p: dict) -> list:
    ev = c.StockLow.model_validate(p)
    return [("manager", f"Мало товара {ev.product_id}",
             f"Осталось {ev.quantity} шт. при минимуме {ev.min_quantity}. Нужна закупка или пополнение.", "")]


RULES: dict[str, Rule] = {c.ORDER_CONFIRMED: _confirmed, c.ORDER_CANCELLED: _cancelled, c.PICKING_COMPLETED: _picked,
                          c.SHIPMENT_DISPATCHED: _dispatched, c.STOCK_LOW: _low}


def handle(s: Session, env: EventEnvelope) -> None:
    for audience, title, message, number in RULES[env.type](env.payload):
        s.add(Notification(event_id=env.event_id, event_type=env.type, audience=audience, title=title,
                           message=message, order_number=number))
        # Здесь была бы отправка e-mail или в мессенджер; пока это строка лога.
        log_event(log, "notification", audience=audience, title=title)


def view(n: Notification) -> dict:
    return {"id": n.id, "type": n.event_type, "audience": n.audience, "title": n.title, "message": n.message,
            "order_number": n.order_number, "created_at": n.created_at, "read": n.read_at is not None}


def build_app(settings: Settings | None = None) -> FastAPI:
    app = create_service(settings or Settings(), title="notification-service",
                         description="Уведомления для менеджеров и сборщиков из событий других сервисов.")
    for event_type in RULES:
        app.state.bus.subscribe(event_type, handle)
    app.state.handlers = {t: handle for t in RULES}
    anyone = require_roles()

    def visible(p: Principal) -> list[str]:
        return ["manager", "picker"] if "admin" in p.roles else p.roles

    @app.get("/notifications", tags=["notifications"], summary="Уведомления для моих ролей, новые сверху")
    def list_notifications(unread_only: bool = False, limit: int = Query(50, ge=1, le=500),
                           p: Principal = Depends(anyone), s: Session = Depends(get_session)):
        q = select(Notification).where(Notification.audience.in_(visible(p))).order_by(Notification.id.desc())
        if unread_only:
            q = q.where(Notification.read_at.is_(None))
        return [view(n) for n in s.scalars(q.limit(limit))]

    @app.post("/notifications/{notification_id}/read", tags=["notifications"])
    def mark_read(notification_id: int, p: Principal = Depends(anyone), s: Session = Depends(get_session)):
        n = s.get(Notification, notification_id)
        if n is None:
            raise NotFound(f"notification {notification_id} not found")
        if n.audience not in visible(p):
            raise Forbidden("this notification is for another role")
        n.read_at = n.read_at or utcnow()
        return view(n)

    return app


app = build_app()
