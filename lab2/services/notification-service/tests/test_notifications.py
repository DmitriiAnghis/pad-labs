import pytest
from fastapi.testclient import TestClient

from notification_service.main import Settings, build_app
from pad_common import contracts as c
from pad_common.events import EventEnvelope, handle_once
from pad_common.testing import generate_keypair, make_token

PRIV, PUB = generate_keypair()


def h(*roles):
    return {"Authorization": f"Bearer {make_token(PRIV, list(roles))}"}


@pytest.fixture()
def client():
    with TestClient(build_app(Settings(database_url="sqlite://", jwt_public_key=PUB, log_format="text"))) as cl:
        yield cl


def deliver(client, event_type, payload):
    env = EventEnvelope(type=event_type, source="t", payload=payload)
    handle_once(client.app.state.db, env, client.app.state.handlers[event_type])
    return env


def test_events_become_notifications_for_roles(client):
    deliver(client, c.ORDER_CONFIRMED, c.OrderConfirmed(order_id="o1", order_number="ORD-1", customer_name="X", lines=[
        c.ConfirmedLine(product_id=1, quantity=2, allocations=[c.Allocation(location_code="A-01", quantity=2)])]).model_dump())
    deliver(client, c.STOCK_LOW, c.StockLow(product_id=7, quantity=1, min_quantity=5).model_dump())
    picker = client.get("/notifications", headers=h("picker")).json()
    assert [n["title"] for n in picker] == ["Новое задание на сборку ORD-1"]
    manager = client.get("/notifications", headers=h("manager")).json()
    assert [n["type"] for n in manager] == ["stock.low", "order.confirmed"]
    assert len(client.get("/notifications", headers=h("admin")).json()) == 3


def test_short_pick_message_and_read_flag(client):
    deliver(client, c.PICKING_COMPLETED, c.PickingCompleted(
        order_id="o1", order_number="ORD-1", picking_id=1, picked_by="ivan", short=True,
        lines=[c.PickedLine(product_id=1, location_code="A", reserved_quantity=3, picked_quantity=1)]).model_dump())
    n = client.get("/notifications", headers=h("manager")).json()[0]
    assert "не хватило 2 шт" in n["message"]
    assert client.post(f"/notifications/{n['id']}/read", headers=h("picker")).status_code == 403
    assert client.post(f"/notifications/{n['id']}/read", headers=h("manager")).json()["read"] is True
    assert client.get("/notifications?unread_only=true", headers=h("manager")).json() == []


def test_duplicate_event_creates_one_notification(client):
    env = deliver(client, c.SHIPMENT_DISPATCHED, c.ShipmentDispatched(order_id="o", order_number="ORD-9", shipment_id=1,
                                                                    carrier="Nova", tracking_number="T").model_dump())
    handle_once(client.app.state.db, env, client.app.state.handlers[c.SHIPMENT_DISPATCHED])
    assert len(client.get("/notifications", headers=h("manager")).json()) == 1
