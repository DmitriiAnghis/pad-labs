"""Слой Services web-ui: логика страниц, которая собирает данные из нескольких репозиториев.

Берёт: репозитории (repositories.py); их передаёт DI (deps.py).
Делает: то, чего нет ни в одном API по отдельности. Например, API остатков отдаёт только
  product_id, а на странице нужно название товара: StockService соединяет остатки
  со справочником товаров. OrderFormService собирает строки заказа из полей формы.
Получается: готовые для шаблона данные.
Зачем: контроллеры остаются тонкими (принять запрос, вызвать сервис, отдать шаблон),
  а эту логику можно проверить тестом без браузера.
"""
from __future__ import annotations

from .api_client import ApiError
from .repositories import (NotificationRepository, OrderRepository, PickingRepository, ProductRepository,
                           StockRepository)


class ProductNames:
    """Справочник id товара -> «SKU · название» для подписей в таблицах."""

    def __init__(self, products: ProductRepository):
        self.products = products

    def load(self) -> dict[int, str]:
        return {p["id"]: f'{p["sku"]} · {p["name"]}' for p in self.products.list()}


class StockService:
    def __init__(self, stock: StockRepository, names: ProductNames):
        self.stock, self.names = stock, names

    def rows(self) -> list[dict]:
        names = self.names.load()
        rows = [dict(r, product=names.get(r["product_id"], f'#{r["product_id"]}')) for r in self.stock.list()]
        return sorted(rows, key=lambda r: (r["product"], r["location_code"]))


class PickingService:
    def __init__(self, pickings: PickingRepository, names: ProductNames):
        self.pickings, self.names = pickings, names

    def detail(self, picking_id: int) -> dict:
        p = self.pickings.get(picking_id)
        names = self.names.load()
        for line in p["lines"]:
            line["product"] = names.get(line["product_id"], f'#{line["product_id"]}')
        return p


class OrderFormService:
    """Превращает поля формы «товар / количество» в строки заказа для order-service."""

    def __init__(self, orders: OrderRepository):
        self.orders = orders

    @staticmethod
    def lines_from_form(product_ids: list[str], quantities: list[str]) -> list[dict]:
        merged: dict[int, int] = {}
        for pid, qty in zip(product_ids, quantities):
            if not pid or not qty:
                continue  # пустая строка формы
            q = int(qty)
            if q <= 0:
                continue
            merged[int(pid)] = merged.get(int(pid), 0) + q  # один товар в двух строках = одна строка
        return [{"product_id": p, "quantity": q} for p, q in merged.items()]

    def create(self, customer_name: str, address: str, product_ids: list[str], quantities: list[str]) -> dict:
        lines = self.lines_from_form(product_ids, quantities)
        if not lines:
            raise ApiError(422, "invalid_request", "order has no lines")
        return self.orders.create(customer_name, address, lines)


class DashboardService:
    """Цифры для главной страницы. Недоступный сервис не роняет страницу: вместо числа будет «—»."""

    def __init__(self, orders: OrderRepository, pickings: PickingRepository, notifications: NotificationRepository):
        self.orders, self.pickings, self.notifications = orders, pickings, notifications

    def summary(self) -> dict:
        def safe(fn):
            try:
                return fn()
            except ApiError:
                return None
        return {
            "orders_confirmed": safe(lambda: len(self.orders.list("confirmed"))),
            "orders_picked": safe(lambda: len(self.orders.list("picked"))),
            "pickings_ready": safe(lambda: len(self.pickings.list("ready"))),
            "pickings_in_progress": safe(lambda: len(self.pickings.list("in_progress"))),
            "unread": safe(lambda: len(self.notifications.list(unread_only=True))),
        }
