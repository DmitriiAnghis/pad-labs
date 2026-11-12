"""Таблица catalog-service: справочник товаров."""
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from pad_common.db import Base
from pad_common.events import utcnow


class Product(Base):
    __tablename__ = "products"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(32), unique=True, index=True)       # артикул
    name: Mapped[str] = mapped_column(String(200))
    barcode: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True, index=True)  # EAN-13 и т.п.
    uom: Mapped[str] = mapped_column(String(16), default="pcs")                 # единица измерения
    weight_kg: Mapped[float] = mapped_column(Float, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
