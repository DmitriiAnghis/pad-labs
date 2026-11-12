"""catalog-service: справочник товаров (SKU, название, штрихкод, единица, вес).

GET    /products                 список и поиск (q по SKU/названию), только активные по умолчанию
POST   /products                 создать (admin, manager)
GET    /products/{id}            карточка товара
GET    /products/by-barcode/{c}  найти по штрихкоду (сканер сборщика)
PATCH  /products/{id}            изменить (admin, manager)
DELETE /products/{id}            снять с учёта: active=false, строка остаётся для истории (admin, manager)

Слои: обработчики ниже (Controller) -> ProductRepository (repository.py, запросы к базе) -> Product (models.py).
Репозиторий обработчик получает через DI: repo: ProductRepository = Depends(product_repo).
"""
from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from pad_common.app import create_service, get_session
from pad_common.auth import Principal, require_roles
from pad_common.config import BaseServiceSettings
from pad_common.logging import log_event

from .models import Product
from .repository import ProductRepository

log = logging.getLogger("catalog")


class Settings(BaseServiceSettings):
    service_name: str = "catalog-service"


class ProductIn(BaseModel):
    sku: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Z0-9-]+$", examples=["SKU-0001"])
    name: str = Field(..., min_length=1, max_length=200, examples=["Коробка картонная 40x30x20"])
    barcode: str | None = Field(None, pattern=r"^[0-9]{8,14}$", examples=["4601234567890"])
    uom: str = Field("pcs", max_length=16)
    weight_kg: float = Field(0.0, ge=0)


class ProductPatch(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    barcode: str | None = Field(None, pattern=r"^[0-9]{8,14}$")
    uom: str | None = Field(None, max_length=16)
    weight_kg: float | None = Field(None, ge=0)
    active: bool | None = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sku: str
    name: str
    barcode: str | None
    uom: str
    weight_kg: float
    active: bool


def product_repo(s: Session = Depends(get_session)) -> ProductRepository:
    """DI: FastAPI создаёт репозиторий на каждый запрос из сессии этого запроса."""
    return ProductRepository(s)


def build_app(settings: Settings | None = None) -> FastAPI:
    app = create_service(settings or Settings(), title="catalog-service",
                         description="Справочник товаров: SKU, название, штрихкод, единица измерения, вес.")
    writers = require_roles("manager")
    readers = require_roles()

    @app.get("/products", response_model=list[ProductOut], tags=["products"])
    def list_products(q: str | None = Query(None, max_length=100), include_inactive: bool = False,
                      limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                      _: Principal = Depends(readers), repo: ProductRepository = Depends(product_repo)):
        return repo.search(q, include_inactive, limit, offset)

    @app.post("/products", response_model=ProductOut, status_code=201, tags=["products"])
    def create_product(body: ProductIn, p: Principal = Depends(writers), repo: ProductRepository = Depends(product_repo)):
        repo.ensure_unique(body.sku, body.barcode)
        prod = repo.add(Product(**body.model_dump()))
        log_event(log, "product created", product_id=prod.id, sku=prod.sku, by=p.username)
        return prod

    @app.get("/products/by-barcode/{barcode}", response_model=ProductOut, tags=["products"])
    def by_barcode(barcode: str, _: Principal = Depends(readers), repo: ProductRepository = Depends(product_repo)):
        return repo.by_barcode(barcode)

    @app.get("/products/{product_id}", response_model=ProductOut, tags=["products"])
    def get_product(product_id: int, _: Principal = Depends(readers), repo: ProductRepository = Depends(product_repo)):
        return repo.get(product_id)

    @app.patch("/products/{product_id}", response_model=ProductOut, tags=["products"])
    def update_product(product_id: int, body: ProductPatch, p: Principal = Depends(writers),
                       repo: ProductRepository = Depends(product_repo)):
        prod = repo.get(product_id)
        data = body.model_dump(exclude_unset=True)
        repo.ensure_unique(None, data.get("barcode"), exclude_id=prod.id)
        for k, v in data.items():
            setattr(prod, k, v)
        log_event(log, "product updated", product_id=prod.id, fields=list(data), by=p.username)
        return prod

    @app.delete("/products/{product_id}", response_model=ProductOut, tags=["products"])
    def deactivate(product_id: int, p: Principal = Depends(writers), repo: ProductRepository = Depends(product_repo)):
        prod = repo.get(product_id)
        prod.active = False
        log_event(log, "product deactivated", product_id=prod.id, by=p.username)
        return prod

    return app


app = build_app()
