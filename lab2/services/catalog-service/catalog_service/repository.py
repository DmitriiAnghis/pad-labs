"""Repository товаров: единственное место в catalog-service, где пишутся запросы к базе.

Берёт: сессию SQLAlchemy (открытую транзакцию к catalog_db), её даёт DI через Depends(get_session).
Делает: поиск, чтение по id и штрихкоду, проверку уникальности, добавление товара.
Получается: объекты Product (models.py) или исключения NotFound/Conflict в общем формате ошибок.
Зачем: обработчики запросов в main.py не содержат SQL и читаются как описание действия
  («найти товар», «проверить, что SKU свободен»). Смена базы или схемы правится только здесь.
"""
from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pad_common.errors import Conflict, NotFound

from .models import Product


class ProductRepository:
    def __init__(self, session: Session):
        self.s = session

    def search(self, q: str | None, include_inactive: bool, limit: int, offset: int) -> list[Product]:
        stmt = select(Product).order_by(Product.sku)
        if not include_inactive:
            stmt = stmt.where(Product.active.is_(True))
        if q:
            like = f"%{q}%"
            stmt = stmt.where(or_(Product.sku.ilike(like), Product.name.ilike(like)))
        return list(self.s.scalars(stmt.limit(limit).offset(offset)))

    def get(self, product_id: int) -> Product:
        p = self.s.get(Product, product_id)
        if p is None:
            raise NotFound(f"product {product_id} not found")
        return p

    def by_barcode(self, barcode: str) -> Product:
        p = self.s.scalar(select(Product).where(Product.barcode == barcode))
        if p is None:
            raise NotFound(f"no product with barcode {barcode}")
        return p

    def ensure_unique(self, sku: str | None, barcode: str | None, exclude_id: int | None = None) -> None:
        """SKU и штрихкод не должны повторяться: иначе 409 duplicate_sku / duplicate_barcode."""
        conds = []
        if sku:
            conds.append(Product.sku == sku)
        if barcode:
            conds.append(Product.barcode == barcode)
        if not conds:
            return
        q = select(Product).where(or_(*conds))
        if exclude_id is not None:
            q = q.where(Product.id != exclude_id)
        other = self.s.scalar(q)
        if other is not None:
            field = "sku" if sku and other.sku == sku else "barcode"
            raise Conflict(f"product with this {field} already exists (id {other.id})", code=f"duplicate_{field}")

    def add(self, product: Product) -> Product:
        self.s.add(product)
        self.s.flush()  # база выдаёт id
        return product
