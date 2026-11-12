"""Таблицы order-service: заказы, строки, история статусов."""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from pad_common.db import Base
from pad_common.events import utcnow


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # uuid4 hex
    number: Mapped[str] = mapped_column(String(32), unique=True)   # ORD-20261105-3F9A1C, для людей
    status: Mapped[str] = mapped_column(String(16), index=True)
    customer_name: Mapped[str] = mapped_column(String(200))
    shipping_address: Mapped[str] = mapped_column(String(500))
    created_by: Mapped[str] = mapped_column(String(64))
    short_picked: Mapped[bool] = mapped_column(Boolean, default=False)
    tracking_number: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    lines: Mapped[list["OrderLine"]] = relationship(back_populates="order", cascade="all, delete-orphan", lazy="selectin")
    history: Mapped[list["StatusChange"]] = relationship(cascade="all, delete-orphan", lazy="selectin",
                                                         order_by="StatusChange.id")


class OrderLine(Base):
    __tablename__ = "order_lines"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    product_id: Mapped[int] = mapped_column(Integer)
    quantity: Mapped[int] = mapped_column(Integer)
    allocations: Mapped[list] = mapped_column(JSON, default=list)  # [{"location_code", "quantity"}] от inventory
    order: Mapped[Order] = relationship(back_populates="lines")


class StatusChange(Base):
    __tablename__ = "order_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"), index=True)
    status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str] = mapped_column(String(200), default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
