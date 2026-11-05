"""Тесты order-service: синхронный резерв, ошибки inventory, компенсация, статусы по событиям."""
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from order_service import domain
from order_service.main import Settings, build_app
from pad_common import contracts as c
from pad_common.events import EventEnvelope, OutboxMessage, handle_once
from pad_common.testing import generate_keypair, make_token

PRIV, PUB = generate_keypair()
calls: list[tuple[str, str]] = []
mode = {"value": "ok"}


def inventory(request: httpx.Request) -> httpx.Response:
    calls.append((request.method, request.url.path))
    if request.method == "DELETE":
        return httpx.Response(200, json={"released_rows": 1})
    if mode["value"] == "conflict":
        return httpx.Response(409, json={"error": {"code": "insufficient_stock", "message": "not enough stock",
                                                   "details": [{"product_id": 1, "requested": 9, "available": 2}]}})
    if mode["value"] == "timeout":
        raise httpx.ReadTimeout("slow", request=request)
    import json
    body = json.loads(request.content)
    return httpx.Response(201, json={"order_id": body["order_id"], "lines": [
        {"product_id": l["product_id"], "quantity": l["quantity"],
         "allocations": [{"location_code": "A-01-01-01", "quantity": l["quantity"]}]} for l in body["lines"]]})


def h(*roles):
    return {"Authorization": f"Bearer {make_token(PRIV, list(roles), username='anna')}"}


ORDER = {"customer_name": "ООО Ромашка", "shipping_address": "Кишинёв", "lines": [{"product_id": 1, "quantity": 2}]}


@pytest.fixture()
def client():
    calls.clear()
    mode["value"] = "ok"
    app = build_app(Settings(database_url="sqlite://", jwt_public_key=PUB, log_format="text"),
                    inventory_transport=httpx.MockTransport(inventory))
    with TestClient(app) as cl:
        yield cl


def outbox(app):
    with app.state.db.session() as s:
        return [o.body for o in s.scalars(select(OutboxMessage).order_by(OutboxMessage.id))]


def test_transitions():
    assert domain.can_move("confirmed", "picked") and domain.can_move("picked", "shipped")
    assert not domain.can_move("shipped", "cancelled") and not domain.can_move("cancelled", "picked")


def test_create_order_reserves_and_writes_outbox(client):
    r = client.post("/orders", headers=h("manager"), json=ORDER)
    assert r.status_code == 201, r.text
    o = r.json()
    assert o["status"] == "confirmed" and o["lines"][0]["allocations"][0]["location_code"] == "A-01-01-01"
    assert calls == [("POST", "/reservations")]
    ev = outbox(client.app)
    assert [e["type"] for e in ev] == ["order.confirmed"]
    assert ev[0]["payload"]["order_number"] == o["number"]


def test_insufficient_stock_returns_409_and_saves_nothing(client):
    mode["value"] = "conflict"
    r = client.post("/orders", headers=h("manager"), json=ORDER)
    assert r.status_code == 409 and r.json()["error"]["details"][0]["available"] == 2
    assert client.get("/orders", headers=h("manager")).json() == [] and outbox(client.app) == []


def test_inventory_timeout_returns_504_and_compensates(client):
    mode["value"] = "timeout"
    r = client.post("/orders", headers=h("manager"), json=ORDER)
    assert r.status_code == 504 and r.json()["error"]["code"] == "upstream_timeout"
    assert calls[-1][0] == "DELETE"  # резерв мог создаться: снимаем
    assert client.get("/orders", headers=h("manager")).json() == []


def test_picker_cannot_create_order(client):
    assert client.post("/orders", headers=h("picker"), json=ORDER).status_code == 403


def test_cancel_and_events_lifecycle(client):
    app = client.app
    o = client.post("/orders", headers=h("manager"), json=ORDER).json()

    def deliver(event_type, payload):
        handle_once(app.state.db, EventEnvelope(type=event_type, source="test", payload=payload), app.state.handlers[event_type])

    deliver(c.PICKING_COMPLETED, c.PickingCompleted(order_id=o["id"], order_number=o["number"], picking_id=1,
                                                    picked_by="ivan", lines=[]).model_dump())
    assert client.get(f"/orders/{o['id']}", headers=h("picker")).json()["status"] == "picked"
    # собранный заказ отменить уже нельзя
    r = client.post(f"/orders/{o['id']}/cancel", headers=h("manager"), json={"reason": "late"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_status"
    deliver(c.SHIPMENT_DISPATCHED, c.ShipmentDispatched(order_id=o["id"], order_number=o["number"], shipment_id=1,
                                                        carrier="Nova", tracking_number="TRK1").model_dump())
    full = client.get(f"/orders/{o['id']}", headers=h("picker")).json()
    assert full["status"] == "shipped" and full["tracking_number"] == "TRK1"
    assert [x["status"] for x in full["history"]] == ["confirmed", "picked", "shipped"]


def test_cancel_confirmed_order_emits_event(client):
    o = client.post("/orders", headers=h("manager"), json=ORDER).json()
    r = client.post(f"/orders/{o['id']}/cancel", headers=h("manager"), json={"reason": "customer"})
    assert r.json()["status"] == "cancelled"
    assert [e["type"] for e in outbox(client.app)] == ["order.confirmed", "order.cancelled"]
    assert client.get("/orders?status=cancelled", headers=h("manager")).json()[0]["id"] == o["id"]
    assert client.get("/orders/nope", headers=h("manager")).status_code == 404
