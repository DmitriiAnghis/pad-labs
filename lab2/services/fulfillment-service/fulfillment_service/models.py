"""Таблицы fulfillment-service: волны, задания на сборку, их строки, отгрузки."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pad_common.db import Base
from pad_common.events import utcnow


class Wave(Base):
    """Волна: несколько заданий, которые собираются за один проход по складу."""
    __tablename__ = "waves"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open / done
    created_by: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    pickings: Mapped[list["Picking"]] = relationship(back_populates="wave", lazy="selectin", order_by="Picking.id")


class Picking(Base):
    """Задание на сборку одного заказа."""
    __tablename__ = "pickings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    order_number: Mapped[str] = mapped_column(String(32))
    customer_name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(16), default="ready", index=True)  # ready / in_progress / done / cancelled
    assigned_to: Mapped[str] = mapped_column(String(64), default="")
    wave_id: Mapped[int | None] = mapped_column(ForeignKey("waves.id"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lines: Mapped[list["PickingLine"]] = relationship(back_populates="picking", cascade="all, delete-orphan",
                                                      lazy="selectin", order_by="PickingLine.sequence")
    wave: Mapped[Wave | None] = relationship(back_populates="pickings")


class PickingLine(Base):
    """Строка маршрута: из какой ячейки взять сколько штук какого товара."""
    __tablename__ = "picking_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    picking_id: Mapped[int] = mapped_column(ForeignKey("pickings.id"), index=True)
    sequence: Mapped[int] = mapped_column(Integer)       # порядок обхода ячеек
    product_id: Mapped[int] = mapped_column(Integer)
    location_code: Mapped[str] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer)
    picked_quantity: Mapped[int] = mapped_column(Integer, default=0)
    picking: Mapped[Picking] = relationship(back_populates="lines")


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), unique=True)
    order_number: Mapped[str] = mapped_column(String(32))
    picking_id: Mapped[int] = mapped_column(ForeignKey("pickings.id"))
    status: Mapped[str] = mapped_column(String(16), default="ready")  # ready / dispatched
    carrier: Mapped[str] = mapped_column(String(64), default="")
    tracking_number: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
