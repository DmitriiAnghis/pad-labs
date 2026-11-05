"""Сквозной (e2e) тест всей системы через gateway.

Берёт: запущенную систему (docker compose up) и пароль администратора.
Делает: проходит реальный сценарий через HTTP: пользователи -> товар -> ячейки -> приёмка ->
заказ -> сборка -> отгрузка, проверяя, что события дошли до других сервисов; плюс проверки
ошибок и одновременных заказов.
Запуск (из папки lab2):
    E2E_BASE_URL=http://localhost:8080 E2E_ADMIN_PASSWORD=<из .env> python -m pytest tests/e2e -v
Без E2E_BASE_URL тест пропускается, поэтому обычный pytest его не трогает.
"""
from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

BASE = os.environ.get("E2E_BASE_URL")
pytestmark = pytest.mark.skipif(not BASE, reason="E2E_BASE_URL is not set (system is not running)")
RUN = uuid.uuid4().hex[:6].upper()


def wait_for(fn, timeout=15.0, step=0.3):
    """События обрабатываются асинхронно: ждём, пока условие станет истинным."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(step)
    raise AssertionError(f"condition not met in {timeout}s, last value: {last!r}")


class Api:
    def __init__(self, token: str | None = None) -> None:
        self.c = httpx.Client(base_url=BASE, timeout=15)
        self.h = {"Authorization": f"Bearer {token}"} if token else {}

    def __getattr__(self, method):
        return lambda path, **kw: self.c.request(method.upper(), f"/api{path}", headers=self.h, **kw)


def login(username, password) -> Api:
    r = Api().post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return Api(r.json()["access_token"])


@pytest.fixture(scope="module")
def users():
    admin = login("admin", os.environ["E2E_ADMIN_PASSWORD"])
    for name, role in (("manager", "manager"), ("picker", "picker")):
        r = admin.post("/users", json={"username": f"{name}-{RUN}", "password": "password-123", "roles": [role]})
        assert r.status_code == 201, r.text
    return {"admin": admin, "manager": login(f"manager-{RUN}", "password-123"),
            "picker": login(f"picker-{RUN}", "password-123")}


def make_product(mgr: Api, locations: list[tuple[str, int]], min_qty: int = 0) -> int:
    pid = mgr.post("/products", json={"sku": f"SKU-{RUN}-{uuid.uuid4().hex[:4].upper()}", "name": "Коробка"}).json()["id"]
    for code, qty in locations:
        mgr.post("/locations", json={"code": code, "zone": code[0]})  # 409, если ячейка уже есть - не важно
        r = mgr.post("/stock/receipts", json={"product_id": pid, "location_code": code, "quantity": qty})
        assert r.status_code == 201, r.text
    if min_qty:
        mgr.put(f"/stock/min-levels/{pid}", json={"min_quantity": min_qty})
    return pid


def test_full_order_lifecycle(users):
    mgr, picker = users["manager"], users["picker"]
    pid = make_product(mgr, [(f"A-{RUN}-01", 2), (f"A-{RUN}-02", 3)], min_qty=4)

    r = mgr.post("/orders", json={"customer_name": "ООО Тест", "shipping_address": "Кишинёв",
                                  "lines": [{"product_id": pid, "quantity": 3}]})
    assert r.status_code == 201, r.text
    order = r.json()
    assert order["status"] == "confirmed"
    assert sum(a["quantity"] for a in order["lines"][0]["allocations"]) == 3

    # order.confirmed -> fulfillment создал задание на сборку
    picking = wait_for(lambda: next((p for p in picker.get("/pickings?status=ready&limit=500").json()
                                     if p["order_id"] == order["id"]), None))
    assert [l["location_code"] for l in picking["lines"]] == [f"A-{RUN}-01", f"A-{RUN}-02"]

    picker.post(f"/pickings/{picking['id']}/start")
    for line in picking["lines"]:
        r = picker.post(f"/pickings/{picking['id']}/lines/{line['id']}/pick",
                        json={"location_code": line["location_code"], "quantity": line["quantity"]})
        assert r.status_code == 200, r.text
    assert picker.post(f"/pickings/{picking['id']}/complete").json()["status"] == "done"

    # picking.completed -> order picked, inventory списал товар, notification сообщил о малом остатке
    wait_for(lambda: mgr.get(f"/orders/{order['id']}").json()["status"] == "picked")
    summary = wait_for(lambda: (s := mgr.get(f"/stock/summary/{pid}").json())["quantity"] == 2 and s)
    assert summary["reserved"] == 0
    wait_for(lambda: any(n["type"] == "stock.low" and f"товара {pid}" in n["title"]
                         for n in mgr.get("/notifications?limit=200").json()))
    assert any(order["number"] in n["title"] for n in picker.get("/notifications?limit=200").json())

    shipment = next(s for s in mgr.get("/shipments?status=ready").json() if s["order_id"] == order["id"])
    assert mgr.post(f"/shipments/{shipment['id']}/dispatch",
                    json={"carrier": "Nova", "tracking_number": f"TRK-{RUN}"}).status_code == 200
    final = wait_for(lambda: (o := mgr.get(f"/orders/{order['id']}").json())["status"] == "shipped" and o)
    assert [h["status"] for h in final["history"]] == ["confirmed", "picked", "shipped"]


def test_cancel_releases_reservation_and_picking(users):
    mgr = users["manager"]
    pid = make_product(mgr, [(f"C-{RUN}-01", 4)])
    order = mgr.post("/orders", json={"customer_name": "X", "shipping_address": "Y",
                                      "lines": [{"product_id": pid, "quantity": 4}]}).json()
    assert mgr.get(f"/stock/summary/{pid}").json()["available"] == 0
    assert mgr.post(f"/orders/{order['id']}/cancel", json={"reason": "e2e"}).json()["status"] == "cancelled"
    wait_for(lambda: mgr.get(f"/stock/summary/{pid}").json()["available"] == 4)
    wait_for(lambda: any(p["order_id"] == order["id"] for p in mgr.get("/pickings?status=cancelled&limit=500").json()))


def test_no_overselling_under_concurrency(users):
    """10 одновременных заказов по 1 штуке при остатке 5: ровно 5 успешных, 5 получают 409."""
    mgr = users["manager"]
    pid = make_product(mgr, [(f"D-{RUN}-01", 3), (f"D-{RUN}-02", 2)])

    def place(_):
        return mgr.post("/orders", json={"customer_name": "race", "shipping_address": "-",
                                         "lines": [{"product_id": pid, "quantity": 1}]}).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        codes = list(pool.map(place, range(10)))
    assert sorted(codes) == [201] * 5 + [409] * 5, codes
    s = mgr.get(f"/stock/summary/{pid}").json()
    assert (s["reserved"], s["available"]) == (5, 0)


def test_errors_are_uniform(users):
    mgr, picker = users["manager"], users["picker"]
    r = Api().get("/orders")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized" and "request_id" in r.json()
    r = picker.post("/orders", json={"customer_name": "x", "shipping_address": "y", "lines": [{"product_id": 1, "quantity": 1}]})
    assert r.status_code == 403
    r = mgr.get("/orders/does-not-exist")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    r = mgr.post("/orders", json={"customer_name": "", "lines": []})
    assert r.status_code == 422 and r.json()["error"]["details"]
    pid = make_product(mgr, [(f"E-{RUN}-01", 1)])
    r = mgr.post("/orders", json={"customer_name": "x", "shipping_address": "y", "lines": [{"product_id": pid, "quantity": 99}]})
    assert r.status_code == 409 and r.json()["error"]["details"][0]["available"] == 1
    assert "Traceback" not in r.text


def test_request_id_is_propagated(users):
    r = users["manager"].c.get("/api/products", headers={**users["manager"].h, "X-Request-ID": f"e2e-{RUN}"})
    assert r.headers["X-Request-ID"] == f"e2e-{RUN}"
