"""Таблицы inventory-service: ячейки, остатки, резервы, движения, минимальные остатки."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, CheckConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pad_common.db import Base
from pad_common.events import utcnow


class Location(Base):
    """Ячейка хранения. Код вида A-01-02-03: зона-ряд-стеллаж-полка."""
    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    zone: Mapped[str] = mapped_column(String(16), default="")
    description: Mapped[str] = mapped_column(String(200), default="")


class StockItem(Base):
    """Сколько штук товара лежит в ячейке и сколько из них уже зарезервировано под заказы."""
    __tablename__ = "stock"
    __table_args__ = (UniqueConstraint("product_id", "location_id"),
                      CheckConstraint("quantity >= 0"), CheckConstraint("reserved >= 0"),
                      CheckConstraint("reserved <= quantity"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, index=True)  # id из catalog-service, без внешнего ключа
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    reserved: Mapped[int] = mapped_column(Integer, default=0)
    location: Mapped[Location] = relationship(lazy="joined")

    @property
    def available(self) -> int:
        return self.quantity - self.reserved


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(36), index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"))
    quantity: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active / consumed / released
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    location: Mapped[Location] = relationship(lazy="joined")


class Movement(Base):
    """Журнал всех изменений остатков: приёмка, перемещение, сборка."""
    __tablename__ = "movements"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(Integer, index=True)
    from_location: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_location: Mapped[str | None] = mapped_column(String(32), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(16))  # receipt / move / pick
    reference: Mapped[str] = mapped_column(String(64), default="")
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MinLevel(Base):
    """Минимальный остаток товара; ниже него inventory публикует событие stock.low."""
    __tablename__ = "min_levels"
    product_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    min_quantity: Mapped[int] = mapped_column(Integer, default=0)
