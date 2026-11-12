"""Repository заказов: единственное место в order-service, где пишутся запросы к базе.

Берёт: сессию SQLAlchemy (открытую транзакцию к orders_db). В обработчиках HTTP её даёт DI
  через Depends(get_session), в обработчиках событий её передаёт шина событий.
Делает: найти заказ по id, выбрать список с фильтром по статусу, сохранить новый заказ.
Получается: объекты Order (models.py) со строками и историей статусов.
Зачем: обработчики в main.py описывают бизнес-шаги (резерв, смена статуса, событие),
  а не SQL. Правила смены статусов лежат отдельно, в domain.py.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from pad_common.errors import NotFound

from .models import Order


class OrderRepository:
    def __init__(self, session: Session):
        self.s = session

    def find(self, order_id: str) -> Order | None:
        """Заказ или None. Обработчики событий молча пропускают чужой или удалённый заказ."""
        return self.s.get(Order, order_id)

    def get(self, order_id: str) -> Order:
        """Заказ или 404 для API."""
        o = self.find(order_id)
        if o is None:
            raise NotFound(f"order {order_id} not found")
        return o

    def list(self, status: str | None, limit: int, offset: int) -> list[Order]:
        q = select(Order).order_by(Order.created_at.desc())
        if status:
            q = q.where(Order.status == status)
        return list(self.s.scalars(q.limit(limit).offset(offset)))

    def add(self, order: Order) -> Order:
        self.s.add(order)
        return order
