"""DI (Dependency Injection, внедрение зависимостей): кто какие объекты получает.

Берёт: запрос пользователя (cookie с токеном), настройки, общий HTTP-клиент.
Делает: описывает, как собрать каждый объект: клиент gateway с токеном -> репозиторий ->
  сервис. Контроллер просто пишет в параметрах `svc: StockService = Depends(stock_service)`,
  и FastAPI сам создаёт всю цепочку для этого запроса.
Получается: контроллеры не создают объекты сами и не знают, откуда берётся токен.
Зачем: в тестах любую зависимость можно подменить через app.dependency_overrides,
  например, подсунуть репозиторий с готовыми данными вместо настоящего gateway.
"""
from __future__ import annotations

from fastapi import Depends, Request

from .api_client import GatewayClient
from .config import Settings
from .repositories import (AuthRepository, LocationRepository, NotificationRepository, OrderRepository,
                           PickingRepository, ProductRepository, ShipmentRepository, StockRepository)
from .services import DashboardService, OrderFormService, PickingService, ProductNames, StockService

TOKEN_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"


class NotLoggedIn(Exception):
    """Нет cookie с токеном: пользователя надо отправить на страницу входа."""


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_api(request: Request, settings: Settings = Depends(get_settings)) -> GatewayClient:
    return GatewayClient(request.app.state.http, settings.gateway_url, request.cookies.get(TOKEN_COOKIE))


def current_user(api: GatewayClient = Depends(get_api)) -> dict:
    """Текущий пользователь (GET /api/auth/me). Истёкший токен даст ApiError 401 -> страница входа."""
    if not api.token:
        raise NotLoggedIn()
    return AuthRepository(api).me()


# --- репозитории ---
def auth_repo(api: GatewayClient = Depends(get_api)) -> AuthRepository:
    return AuthRepository(api)


def product_repo(api: GatewayClient = Depends(get_api)) -> ProductRepository:
    return ProductRepository(api)


def location_repo(api: GatewayClient = Depends(get_api)) -> LocationRepository:
    return LocationRepository(api)


def stock_repo(api: GatewayClient = Depends(get_api)) -> StockRepository:
    return StockRepository(api)


def order_repo(api: GatewayClient = Depends(get_api)) -> OrderRepository:
    return OrderRepository(api)


def picking_repo(api: GatewayClient = Depends(get_api)) -> PickingRepository:
    return PickingRepository(api)


def shipment_repo(api: GatewayClient = Depends(get_api)) -> ShipmentRepository:
    return ShipmentRepository(api)


def notification_repo(api: GatewayClient = Depends(get_api)) -> NotificationRepository:
    return NotificationRepository(api)


# --- сервисы (собираются из репозиториев) ---
def product_names(products: ProductRepository = Depends(product_repo)) -> ProductNames:
    return ProductNames(products)


def stock_service(stock: StockRepository = Depends(stock_repo), names: ProductNames = Depends(product_names)) -> StockService:
    return StockService(stock, names)


def picking_service(pickings: PickingRepository = Depends(picking_repo),
                    names: ProductNames = Depends(product_names)) -> PickingService:
    return PickingService(pickings, names)


def order_form_service(orders: OrderRepository = Depends(order_repo)) -> OrderFormService:
    return OrderFormService(orders)


def dashboard_service(orders: OrderRepository = Depends(order_repo), pickings: PickingRepository = Depends(picking_repo),
                      notifications: NotificationRepository = Depends(notification_repo)) -> DashboardService:
    return DashboardService(orders, pickings, notifications)
