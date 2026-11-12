"""Тесты inventory-service: приёмка, резерв «всё или ничего», идемпотентность, списание после сборки."""
import httpx
import pytest
from fastapi.testclient import TestClient

from inventory_service.main import Settings, build_app
from inventory_service.models import Movement
from pad_common import contracts as c
from pad_common.events import EventEnvelope, OutboxMessage, handle_once
from pad_common.testing import generate_keypair, make_token
from sqlalchemy import select

PRIV, PUB = generate_keypair()


def catalog(request: httpx.Request) -> httpx.Response:
    pid = int(request.url.path.rsplit("/", 1)[-1])
    if pid == 404:
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "product 404 not found"}})
    if pid == 500:
        return httpx.Response(500)
    return httpx.Response(200, json={"id": pid, "active": pid != 13})


def h(*roles):
    return {"Authorization": f"Bearer {make_token(PRIV, list(roles), username='mgr')}"}


@pytest.fixture()
def c_():
    app = build_app(Settings(database_url="sqlite://", jwt_public_key=PUB, log_format="text"),
                    catalog_transport=httpx.MockTransport(catalog))
    with TestClient(app) as client:
        for code in ("A-01-01-01", "A-01-01-02", "B-01-01-01"):
            assert client.post("/locations", headers=h("manager"), json={"code": code, "zone": code[0]}).status_code == 201
        yield client


def receive(c_, pid, loc, qty):
    r = c_.post("/stock/receipts", headers=h("manager"), json={"product_id": pid, "location_code": loc, "quantity": qty})
    assert r.status_code == 201, r.text
    return r.json()


def test_receipt_checks_catalog(c_):
    assert receive(c_, 1, "A-01-01-01", 5)["available"] == 5
    assert c_.post("/stock/receipts", headers=h("manager"),
                   json={"product_id": 404, "location_code": "A-01-01-01", "quantity": 1}).status_code == 404
    r = c_.post("/stock/receipts", headers=h("manager"), json={"product_id": 13, "location_code": "A-01-01-01", "quantity": 1})
    assert r.status_code == 422 and r.json()["error"]["code"] == "product_inactive"
    r = c_.post("/stock/receipts", headers=h("manager"), json={"product_id": 500, "location_code": "A-01-01-01", "quantity": 1})
    assert r.status_code == 503 and r.json()["error"]["code"] == "upstream_unavailable"
    r = c_.post("/stock/receipts", headers=h("manager"), json={"product_id": 1, "location_code": "Z-99", "quantity": 1})
    assert r.status_code == 404 and r.json()["error"]["code"] == "location_not_found"


def test_reserve_splits_across_locations_in_code_order(c_):
    receive(c_, 1, "A-01-01-02", 3)
    receive(c_, 1, "A-01-01-01", 2)
    r = c_.post("/reservations", headers=h("manager"), json={"order_id": "o1", "lines": [{"product_id": 1, "quantity": 4}]})
    assert r.status_code == 201, r.text
    alloc = r.json()["lines"][0]["allocations"]
    assert alloc == [{"location_code": "A-01-01-01", "quantity": 2}, {"location_code": "A-01-01-02", "quantity": 2}]
    s = c_.get("/stock/summary/1", headers=h("picker")).json()
    assert (s["quantity"], s["reserved"], s["available"]) == (5, 4, 1)


def test_reserve_is_all_or_nothing(c_):
    receive(c_, 1, "A-01-01-01", 5)
    receive(c_, 2, "A-01-01-01", 1)
    r = c_.post("/reservations", headers=h("manager"),
                json={"order_id": "o2", "lines": [{"product_id": 1, "quantity": 2}, {"product_id": 2, "quantity": 3}]})
    assert r.status_code == 409
    assert r.json()["error"]["details"] == [{"product_id": 2, "requested": 3, "available": 1}]
    # товар 1 тоже не зарезервирован: транзакция откатилась целиком
    assert c_.get("/stock/summary/1", headers=h("picker")).json()["reserved"] == 0


def test_reserve_is_idempotent_and_release(c_):
    receive(c_, 1, "A-01-01-01", 5)
    body = {"order_id": "o3", "lines": [{"product_id": 1, "quantity": 2}]}
    c_.post("/reservations", headers=h("manager"), json=body)
    c_.post("/reservations", headers=h("manager"), json=body)  # повтор после таймаута
    assert c_.get("/stock/summary/1", headers=h("picker")).json()["reserved"] == 2
    assert c_.delete("/reservations/o3", headers=h("manager")).json()["released_rows"] == 1
    assert c_.delete("/reservations/o3", headers=h("manager")).json()["released_rows"] == 0
    assert c_.get("/stock/summary/1", headers=h("picker")).json()["reserved"] == 0


def test_move_respects_reserved(c_):
    receive(c_, 1, "A-01-01-01", 5)
    c_.post("/reservations", headers=h("manager"), json={"order_id": "o4", "lines": [{"product_id": 1, "quantity": 4}]})
    r = c_.post("/stock/moves", headers=h("picker"),
                json={"product_id": 1, "from_location_code": "A-01-01-01", "to_location_code": "B-01-01-01", "quantity": 2})
    assert r.status_code == 409
    r = c_.post("/stock/moves", headers=h("picker"),
                json={"product_id": 1, "from_location_code": "A-01-01-01", "to_location_code": "B-01-01-01", "quantity": 1})
    assert r.status_code == 204


def test_picking_completed_event_consumes_stock_and_emits_stock_low(c_):
    app = c_.app
    receive(c_, 1, "A-01-01-01", 5)
    c_.put("/stock/min-levels/1", headers=h("manager"), json={"min_quantity": 3})
    c_.post("/reservations", headers=h("manager"), json={"order_id": "o5", "lines": [{"product_id": 1, "quantity": 3}]})
    ev = c.PickingCompleted(order_id="o5", order_number="ORD-5", picking_id=1, picked_by="ivan", short=True,
                            lines=[c.PickedLine(product_id=1, location_code="A-01-01-01", reserved_quantity=3, picked_quantity=2)])
    env = EventEnvelope(type=c.PICKING_COMPLETED, source="test", payload=ev.model_dump())
    assert handle_once(app.state.db, env, app.state.handlers[c.PICKING_COMPLETED])
    assert not handle_once(app.state.db, env, app.state.handlers[c.PICKING_COMPLETED])  # дубль пропущен
    s = c_.get("/stock/summary/1", headers=h("picker")).json()
    assert (s["quantity"], s["reserved"]) == (3, 0)  # списано 2, недобор 1 вернулся в свободный остаток
    with app.state.db.session() as db:
        assert [m.kind for m in db.scalars(select(Movement))] == ["receipt", "pick"]
        out = list(db.scalars(select(OutboxMessage)))
    assert out == []  # остаток 3 не ниже минимума 3, события нет
    # спишем ещё одну штуку: остаток 2 < 3 -> событие stock.low
    c_.post("/reservations", headers=h("manager"), json={"order_id": "o6", "lines": [{"product_id": 1, "quantity": 1}]})
    ev2 = ev.model_copy(update={"order_id": "o6", "short": False,
                                "lines": [c.PickedLine(product_id=1, location_code="A-01-01-01", reserved_quantity=1, picked_quantity=1)]})
    handle_once(app.state.db, EventEnvelope(type=c.PICKING_COMPLETED, source="test", payload=ev2.model_dump()),
                app.state.handlers[c.PICKING_COMPLETED])
    with app.state.db.session() as db:
        out = [o.body for o in db.scalars(select(OutboxMessage))]
    assert [o["type"] for o in out] == ["stock.low"]
    assert out[0]["payload"] == {"product_id": 1, "quantity": 2, "min_quantity": 3}


def test_order_cancelled_event_releases(c_):
    app = c_.app
    receive(c_, 1, "A-01-01-01", 5)
    c_.post("/reservations", headers=h("manager"), json={"order_id": "o7", "lines": [{"product_id": 1, "quantity": 5}]})
    env = EventEnvelope(type=c.ORDER_CANCELLED, source="test",
                        payload=c.OrderCancelled(order_id="o7", order_number="ORD-7").model_dump())
    handle_once(app.state.db, env, app.state.handlers[c.ORDER_CANCELLED])
    assert c_.get("/stock/summary/1", headers=h("picker")).json()["available"] == 5


def test_picker_cannot_reserve(c_):
    r = c_.post("/reservations", headers=h("picker"), json={"order_id": "x", "lines": [{"product_id": 1, "quantity": 1}]})
    assert r.status_code == 403
