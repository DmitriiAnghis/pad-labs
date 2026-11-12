"""Controller в схеме MVC: принимает действие пользователя и выбирает, что показать.

Берёт: запрос браузера (открыл страницу или отправил форму).
Делает: получает через DI (deps.py) нужный репозиторий или сервис, вызывает его и
  - для GET отдаёт View: шаблон из templates/ с данными (Model);
  - для POST после изменения перенаправляет браузер на страницу (схема Post/Redirect/Get:
    обновление страницы не отправит форму второй раз), текст результата идёт в ?ok=...
Получается: HTML-страница или перенаправление.
Ошибки API (нет товара, не та роль, сервис недоступен) здесь не ловятся: их единообразно
обрабатывает main.py (функция api_error_handler).

MVC в web-ui:
  Model      = repositories.py + services.py (данные и правила, без HTML);
  View       = templates/*.html (только вывод, без запросов к API);
  Controller = этот файл (связывает действие пользователя с Model и View).
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import deps
from .config import Settings
from .repositories import (AuthRepository, LocationRepository, NotificationRepository, OrderRepository,
                           PickingRepository, ProductRepository, ShipmentRepository)
from .services import DashboardService, OrderFormService, PickingService, StockService
from .view_helpers import register_filters

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
register_filters(templates.env)


def view(request: Request, name: str, user: dict | None, **ctx):
    """Отдать View (шаблон) с данными. Флаги ролей нужны шаблону, чтобы прятать недоступные кнопки.

    Скрытая кнопка не защита: права всё равно проверяет бизнес-сервис по токену (ответ 403).
    """
    roles = set(user["roles"]) if user else set()
    return templates.TemplateResponse(request, name, {
        "user": user, "is_manager": bool(roles & {"admin", "manager"}),
        "is_picker": bool(roles & {"admin", "picker"}), **ctx})


def go(url: str, ok: str | None = None) -> RedirectResponse:
    if ok:
        url += ("&" if "?" in url else "?") + "ok=" + quote(ok)
    return RedirectResponse(url, status_code=303)


# ---------- вход и выход ----------
@router.get("/login")
def login_page(request: Request):
    return view(request, "login.html", None)


@router.post("/login")
def login(username: str = Form(...), password: str = Form(...), auth: AuthRepository = Depends(deps.auth_repo),
          settings: Settings = Depends(deps.get_settings)):
    tokens = auth.login(username, password)
    resp = go("/")
    opts = dict(httponly=True, samesite="lax", secure=settings.cookie_secure)
    resp.set_cookie(deps.TOKEN_COOKIE, tokens["access_token"], max_age=tokens["expires_in"], **opts)
    resp.set_cookie(deps.REFRESH_COOKIE, tokens["refresh_token"], max_age=7 * 24 * 3600, **opts)
    return resp


@router.post("/logout")
def logout(request: Request, auth: AuthRepository = Depends(deps.auth_repo)):
    refresh = request.cookies.get(deps.REFRESH_COOKIE)
    if refresh:
        try:
            auth.logout(refresh)  # отзываем refresh-токен в auth-service
        except Exception:  # noqa: BLE001 - выйти из интерфейса нужно даже при недоступном auth-service
            pass
    resp = go("/login")
    resp.delete_cookie(deps.TOKEN_COOKIE)
    resp.delete_cookie(deps.REFRESH_COOKIE)
    return resp


# ---------- главная ----------
@router.get("/")
def dashboard(request: Request, user: dict = Depends(deps.current_user),
              svc: DashboardService = Depends(deps.dashboard_service)):
    return view(request, "dashboard.html", user, summary=svc.summary())


# ---------- товары ----------
@router.get("/products")
def products_page(request: Request, q: str = "", user: dict = Depends(deps.current_user),
                  products: ProductRepository = Depends(deps.product_repo)):
    return view(request, "products.html", user, products=products.list(q or None), q=q)


@router.post("/products")
def products_create(sku: str = Form(...), name: str = Form(...), barcode: str = Form(""), uom: str = Form("pcs"),
                    _: dict = Depends(deps.current_user), products: ProductRepository = Depends(deps.product_repo)):
    p = products.create(sku.strip(), name.strip(), barcode.strip(), uom.strip())
    return go("/products", f"Товар {p['sku']} создан, id {p['id']}")


# ---------- ячейки и остатки ----------
@router.get("/stock")
def stock_page(request: Request, user: dict = Depends(deps.current_user), svc: StockService = Depends(deps.stock_service),
               locations: LocationRepository = Depends(deps.location_repo),
               products: ProductRepository = Depends(deps.product_repo)):
    return view(request, "stock.html", user, rows=svc.rows(), locations=locations.list(), products=products.list())


@router.post("/locations")
def locations_create(code: str = Form(...), zone: str = Form(""), _: dict = Depends(deps.current_user),
                     locations: LocationRepository = Depends(deps.location_repo)):
    loc = locations.create(code.strip(), zone.strip())
    return go("/stock", f"Ячейка {loc['code']} создана")


@router.post("/stock/receipts")
def stock_receive(product_id: int = Form(...), location_code: str = Form(...), quantity: int = Form(...),
                  _: dict = Depends(deps.current_user), svc: StockService = Depends(deps.stock_service)):
    svc.stock.receive(product_id, location_code, quantity)
    return go("/stock", f"Принято {quantity} шт. в ячейку {location_code}")


@router.post("/stock/moves")
def stock_move(product_id: int = Form(...), from_code: str = Form(...), to_code: str = Form(...),
               quantity: int = Form(...), _: dict = Depends(deps.current_user),
               svc: StockService = Depends(deps.stock_service)):
    svc.stock.move(product_id, from_code, to_code, quantity)
    return go("/stock", f"Перемещено {quantity} шт. из {from_code} в {to_code}")


# ---------- заказы ----------
@router.get("/orders")
def orders_page(request: Request, status: str = "", user: dict = Depends(deps.current_user),
                orders: OrderRepository = Depends(deps.order_repo), products: ProductRepository = Depends(deps.product_repo)):
    return view(request, "orders.html", user, orders=orders.list(status or None), status=status,
                products=products.list())


@router.post("/orders")
def orders_create(customer_name: str = Form(...), shipping_address: str = Form(...),
                  product_id: list[str] = Form(...), quantity: list[str] = Form(...),
                  _: dict = Depends(deps.current_user), svc: OrderFormService = Depends(deps.order_form_service)):
    o = svc.create(customer_name, shipping_address, product_id, quantity)
    return go(f"/orders/{o['id']}", f"Заказ {o['number']} создан, товар зарезервирован")


@router.get("/orders/{order_id}")
def order_detail(request: Request, order_id: str, user: dict = Depends(deps.current_user),
                 orders: OrderRepository = Depends(deps.order_repo), names=Depends(deps.product_names)):
    return view(request, "order_detail.html", user, order=orders.get(order_id), names=names.load())


@router.post("/orders/{order_id}/cancel")
def order_cancel(order_id: str, reason: str = Form(""), _: dict = Depends(deps.current_user),
                 orders: OrderRepository = Depends(deps.order_repo)):
    orders.cancel(order_id, reason)
    return go(f"/orders/{order_id}", "Заказ отменён, резерв снят")


# ---------- сборка ----------
@router.get("/pickings")
def pickings_page(request: Request, status: str = "", user: dict = Depends(deps.current_user),
                  pickings: PickingRepository = Depends(deps.picking_repo)):
    return view(request, "pickings.html", user, pickings=pickings.list(status or None), status=status)


@router.get("/pickings/{picking_id}")
def picking_detail(request: Request, picking_id: int, user: dict = Depends(deps.current_user),
                   svc: PickingService = Depends(deps.picking_service)):
    return view(request, "picking_detail.html", user, p=svc.detail(picking_id))


@router.post("/pickings/{picking_id}/start")
def picking_start(picking_id: int, _: dict = Depends(deps.current_user),
                  pickings: PickingRepository = Depends(deps.picking_repo)):
    pickings.start(picking_id)
    return go(f"/pickings/{picking_id}", "Задание взято в работу")


@router.post("/pickings/{picking_id}/lines/{line_id}/pick")
def picking_pick(picking_id: int, line_id: int, location_code: str = Form(...), quantity: int = Form(...),
                 _: dict = Depends(deps.current_user), pickings: PickingRepository = Depends(deps.picking_repo)):
    pickings.pick(picking_id, line_id, location_code.strip(), quantity)
    return go(f"/pickings/{picking_id}", f"Ячейка {location_code}: отмечено {quantity} шт.")


@router.post("/pickings/{picking_id}/complete")
def picking_complete(picking_id: int, _: dict = Depends(deps.current_user),
                     pickings: PickingRepository = Depends(deps.picking_repo)):
    pickings.complete(picking_id)
    return go(f"/pickings/{picking_id}", "Сборка завершена, создана отгрузка")


# ---------- отгрузки ----------
@router.get("/shipments")
def shipments_page(request: Request, user: dict = Depends(deps.current_user),
                   shipments: ShipmentRepository = Depends(deps.shipment_repo)):
    return view(request, "shipments.html", user, shipments=shipments.list())


@router.post("/shipments/{shipment_id}/dispatch")
def shipment_dispatch(shipment_id: int, carrier: str = Form(...), tracking_number: str = Form(...),
                      _: dict = Depends(deps.current_user), shipments: ShipmentRepository = Depends(deps.shipment_repo)):
    shipments.dispatch(shipment_id, carrier.strip(), tracking_number.strip())
    return go("/shipments", "Отгрузка отправлена")


# ---------- уведомления ----------
@router.get("/notifications")
def notifications_page(request: Request, user: dict = Depends(deps.current_user),
                       notifications: NotificationRepository = Depends(deps.notification_repo)):
    return view(request, "notifications.html", user, items=notifications.list())


@router.post("/notifications/{notification_id}/read")
def notification_read(notification_id: int, _: dict = Depends(deps.current_user),
                      notifications: NotificationRepository = Depends(deps.notification_repo)):
    notifications.mark_read(notification_id)
    return go("/notifications")
