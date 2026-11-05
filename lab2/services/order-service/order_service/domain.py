"""Статусы заказа и разрешённые переходы между ними.

confirmed --(сборка завершена)--> picked --(отгрузка уехала)--> shipped
confirmed --(отмена)--> cancelled
Заказ в статусе new не хранится: если резерв не удался, заказа нет вообще.
"""
from __future__ import annotations

CONFIRMED, PICKED, SHIPPED, CANCELLED = "confirmed", "picked", "shipped", "cancelled"

TRANSITIONS: dict[str, set[str]] = {
    CONFIRMED: {PICKED, CANCELLED},
    PICKED: {SHIPPED},
    SHIPPED: set(),
    CANCELLED: set(),
}


def can_move(current: str, new: str) -> bool:
    return new in TRANSITIONS.get(current, set())
