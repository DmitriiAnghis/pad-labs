"""Тесты web-ui без Docker: настоящий gateway подменён httpx.MockTransport.

Проверяют: вход ставит cookie, страницы рендерятся с данными из API, ошибка API показывается
текстом на странице, без входа идёт перенаправление на /login, Services собирают данные верно.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from web_ui.config import Settings
from web_ui.main import build_app
from web_ui.services import OrderFormService

PRODUCTS = [{"id": 1, "sku": "SKU-1", "name": "Кружка", "barcode": None, "uom": "pcs", "weight_kg": 0, "active": True}]
STOCK = [{"product_id": 1, "location_code": "A-01", "quantity": 5, "reserved": 2, "available": 3}]


def fake_gateway(request: httpx.Request) -> httpx.Response:
    path, method = request.url.path, request.method
    if path == "/api/auth/login":
        body = json.loads(request.content)
        if body["password"] != "secret":
            return httpx.Response(401, json={"error": {"code": "invalid_credentials", "message": "wrong username or password"}})
        return httpx.Response(200, json={"access_token": "tok", "refresh_token": "ref", "expires_in": 900})
    if request.headers.get("Authorization") != "Bearer tok":
        return httpx.Response(401, json={"error": {"code": "unauthorized", "message": "missing bearer token"}})
    if path == "/api/auth/me":
        return httpx.Response(200, json={"id": 1, "username": "manager1", "roles": ["manager"]})
    if path == "/api/products":
        return httpx.Response(200, json=PRODUCTS)
    if path == "/api/stock":
        return httpx.Response(200, json=STOCK)
    if path == "/api/locations":
        return httpx.Response(200, json=[{"id": 1, "code": "A-01", "zone": "A"}])
    if path == "/api/orders" and method == "POST":
        return httpx.Response(409, json={"error": {"code": "insufficient_stock", "message": "not enough stock",
                                                   "details": [{"product_id": 1, "requested": 9, "available": 3}]}})
    if path in ("/api/orders", "/api/pickings", "/api/notifications"):
        return httpx.Response(200, json=[])
    return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


@pytest.fixture
def client():
    app = build_app(Settings(gateway_url="http://gw", log_format="text"), transport=httpx.MockTransport(fake_gateway))
    with TestClient(app) as c:
        yield c


def login(c: TestClient):
    r = c.post("/login", data={"username": "manager1", "password": "secret"}, follow_redirects=False)
    assert r.status_code == 303 and r.cookies.get("access_token") == "tok"


def test_without_login_redirects_to_login(client):
    r = client.get("/stock", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_wrong_password_shows_error(client):
    r = client.post("/login", data={"username": "x", "password": "bad"})
    assert "Неверный логин или пароль" in r.text


def test_stock_page_joins_product_names(client):
    login(client)
    r = client.get("/stock")
    assert r.status_code == 200
    assert "SKU-1 · Кружка" in r.text and "A-01" in r.text


def test_api_error_is_shown_after_form(client):
    login(client)
    r = client.post("/orders", data={"customer_name": "c", "shipping_address": "a", "product_id": ["1"], "quantity": ["9"]},
                    headers={"referer": "http://testserver/orders"})
    assert r.url.path == "/orders"
    assert "Не хватает свободного товара" in r.text and "нужно 9, свободно 3" in r.text


def test_health(client):
    assert client.get("/health").json()["status"] == "ok"


def test_order_form_lines_merge_and_skip_empty():
    lines = OrderFormService.lines_from_form(["1", "", "1", "2"], ["2", "", "3", "0"])
    assert lines == [{"product_id": 1, "quantity": 5}]
