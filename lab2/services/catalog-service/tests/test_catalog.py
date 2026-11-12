import pytest
from fastapi.testclient import TestClient

from catalog_service.main import Settings, build_app
from pad_common.testing import generate_keypair, make_token

PRIV, PUB = generate_keypair()


def h(*roles):
    return {"Authorization": f"Bearer {make_token(PRIV, list(roles))}"}


@pytest.fixture()
def c():
    with TestClient(build_app(Settings(database_url="sqlite://", jwt_public_key=PUB, log_format="text"))) as client:
        yield client


def new(c, sku="SKU-1", barcode="4601234567890"):
    r = c.post("/products", headers=h("manager"), json={"sku": sku, "name": "Box", "barcode": barcode})
    assert r.status_code == 201, r.text
    return r.json()


def test_crud_and_search(c):
    p = new(c)
    assert c.get(f"/products/{p['id']}", headers=h("picker")).json()["sku"] == "SKU-1"
    assert c.get("/products/by-barcode/4601234567890", headers=h("picker")).json()["id"] == p["id"]
    assert len(c.get("/products?q=box", headers=h("picker")).json()) == 1
    r = c.patch(f"/products/{p['id']}", headers=h("manager"), json={"name": "Big box"})
    assert r.json()["name"] == "Big box"
    c.delete(f"/products/{p['id']}", headers=h("manager"))
    assert c.get("/products", headers=h("picker")).json() == []
    assert len(c.get("/products?include_inactive=true", headers=h("picker")).json()) == 1


def test_duplicates_are_409(c):
    new(c)
    r = c.post("/products", headers=h("manager"), json={"sku": "SKU-1", "name": "x"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_sku"
    r = c.post("/products", headers=h("manager"), json={"sku": "SKU-2", "name": "x", "barcode": "4601234567890"})
    assert r.json()["error"]["code"] == "duplicate_barcode"


def test_picker_cannot_create(c):
    r = c.post("/products", headers=h("picker"), json={"sku": "SKU-9", "name": "x"})
    assert r.status_code == 403


def test_not_found_and_validation(c):
    assert c.get("/products/999", headers=h("picker")).status_code == 404
    r = c.post("/products", headers=h("admin"), json={"sku": "bad sku", "name": ""})
    assert r.status_code == 422
