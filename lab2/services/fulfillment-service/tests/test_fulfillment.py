"""Тесты fulfillment-service: задание из события, сканирование ячеек, недобор, волна, отгрузка."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from fulfillment_service.logic import location_key
from fulfillment_service.main import Settings, build_app
from pad_common import contracts as c
from pad_common.events import EventEnvelope, OutboxMessage, handle_once
from pad_common.testing import generate_keypair, make_token

PRIV, PUB = generate_keypair()


def h(roles, user="ivan"):
    return {"Authorization": f"Bearer {make_token(PRIV, list(roles), username=user)}"}


PICKER, MANAGER = h(["picker"]), h(["manager"], "anna")


@pytest.fixture()
def client():
    with TestClient(build_app(Settings(database_url="sqlite://", jwt_public_key=PUB, log_format="text"))) as cl:
        yield cl


def confirm(client, order_id, lines):
    ev = c.OrderConfirmed(order_id=order_id, order_number=f"ORD-{order_id}", customer_name="X", lines=[
        c.ConfirmedLine(product_id=pid, quantity=sum(q for _, q in allocs),
                        allocations=[c.Allocation(location_code=l, quantity=q) for l, q in allocs]) for pid, allocs in lines])
    env = EventEnvelope(type=c.ORDER_CONFIRMED, source="test", payload=ev.model_dump())
    return handle_once(client.app.state.db, env, client.app.state.handlers[c.ORDER_CONFIRMED])


def outbox(client):
    with client.app.state.db.session() as s:
        return [o.body for o in s.scalars(select(OutboxMessage).order_by(OutboxMessage.id))]


def test_location_order_is_numeric():
    codes = ["A-01-10-01", "A-01-02-03", "B-01-01-01", "A-01-02-01"]
    assert sorted(codes, key=location_key) == ["A-01-02-01", "A-01-02-03", "A-01-10-01", "B-01-01-01"]


def test_order_confirmed_creates_route_sorted_by_location(client):
    confirm(client, "o1", [(1, [("B-01-01-01", 1), ("A-01-10-01", 2)]), (2, [("A-01-02-01", 3)])])
    p = client.get("/pickings", headers=PICKER).json()[0]
    assert [l["location_code"] for l in p["lines"]] == ["A-01-02-01", "A-01-10-01", "B-01-01-01"]
    # то же событие о заказе с другим event_id (повторная отправка) второе задание не создаёт
    confirm(client, "o1", [(1, [("A-01-01-01", 1)])])
    assert len(client.get("/pickings", headers=PICKER).json()) == 1


def test_full_picking_flow_with_short_pick(client):
    confirm(client, "o2", [(1, [("A-01-01-01", 2)]), (2, [("A-01-01-02", 1)])])
    pid = client.get("/pickings", headers=PICKER).json()[0]["id"]
    # без старта отмечать нельзя
    assert client.post(f"/pickings/{pid}/complete", headers=PICKER).status_code == 409
    lines = client.post(f"/pickings/{pid}/start", headers=PICKER).json()["lines"]
    # другой сборщик не может работать с чужим заданием
    assert client.post(f"/pickings/{pid}/lines/{lines[0]['id']}/pick", headers=h(["picker"], "petr"),
                       json={"location_code": "A-01-01-01", "quantity": 1}).status_code == 403
    r = client.post(f"/pickings/{pid}/lines/{lines[0]['id']}/pick", headers=PICKER, json={"location_code": "A-01-01-02", "quantity": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "wrong_location"
    r = client.post(f"/pickings/{pid}/lines/{lines[0]['id']}/pick", headers=PICKER, json={"location_code": "A-01-01-01", "quantity": 3})
    assert r.json()["error"]["code"] == "over_pick"
    client.post(f"/pickings/{pid}/lines/{lines[0]['id']}/pick", headers=PICKER, json={"location_code": "A-01-01-01", "quantity": 2})
    # вторую строку не нашли (недобор) и завершаем
    done = client.post(f"/pickings/{pid}/complete", headers=PICKER).json()
    assert done["status"] == "done"
    ev = outbox(client)[-1]
    assert ev["type"] == "picking.completed" and ev["payload"]["short"] is True
    assert ev["payload"]["lines"][1]["picked_quantity"] == 0
    # появилась отгрузка, менеджер её отправляет
    sh = client.get("/shipments", headers=MANAGER).json()[0]
    assert client.post(f"/shipments/{sh['id']}/dispatch", headers=PICKER,
                       json={"carrier": "Nova", "tracking_number": "T1"}).status_code == 403
    r = client.post(f"/shipments/{sh['id']}/dispatch", headers=MANAGER, json={"carrier": "Nova", "tracking_number": "T1"})
    assert r.json()["status"] == "dispatched"
    assert client.post(f"/shipments/{sh['id']}/dispatch", headers=MANAGER,
                       json={"carrier": "Nova", "tracking_number": "T1"}).status_code == 409
    assert outbox(client)[-1]["type"] == "shipment.dispatched"


def test_wave_combines_route(client):
    confirm(client, "o3", [(1, [("A-01-01-01", 2)])])
    confirm(client, "o4", [(1, [("A-01-01-01", 1)]), (2, [("A-01-01-02", 4)])])
    r = client.post("/waves", headers=MANAGER, json={"max_pickings": 5})
    assert r.status_code == 201
    route = r.json()["route"]
    assert route[0] == {"location_code": "A-01-01-01", "product_id": 1, "quantity": 3, "orders": {"ORD-o3": 2, "ORD-o4": 1}}
    assert route[1]["quantity"] == 4
    # все задания уже в волне
    assert client.post("/waves", headers=MANAGER, json={}).status_code == 409
    assert client.post("/waves", headers=PICKER, json={}).status_code == 403


def test_order_cancelled_cancels_picking(client):
    confirm(client, "o5", [(1, [("A-01-01-01", 1)])])
    env = EventEnvelope(type=c.ORDER_CANCELLED, source="t", payload=c.OrderCancelled(order_id="o5", order_number="ORD-o5").model_dump())
    handle_once(client.app.state.db, env, client.app.state.handlers[c.ORDER_CANCELLED])
    assert client.get("/pickings?status=cancelled", headers=PICKER).json()[0]["order_id"] == "o5"
