"""Тесты gateway: маршрутизация, проверка JWT, 401/429/503/504, подмена заголовков."""
import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.config import Settings
from gateway.main import build_app
from gateway.routes import build_routes, match
from pad_common.testing import generate_keypair, make_token

PRIV, PUB = generate_keypair()
seen: list[httpx.Request] = []


def upstream(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    if request.url.host == "slow":
        raise httpx.ReadTimeout("slow", request=request)
    if request.url.host == "down":
        raise httpx.ConnectError("down", request=request)
    return httpx.Response(200, json={"host": request.url.host, "path": request.url.path,
                                     "query": str(request.url.query, "ascii")})


def make(**over):
    s = Settings(jwt_public_key=PUB, log_format="text", auth_url="http://auth", catalog_url="http://catalog",
                 rag_url="http://rag", order_url="http://slow", inventory_url="http://down", **over)
    return TestClient(build_app(s, transport=httpx.MockTransport(upstream)))


@pytest.fixture()
def client():
    seen.clear()
    with make() as c:
        yield c


def bearer(roles=("manager",)):
    return {"Authorization": f"Bearer {make_token(PRIV, list(roles), user_id='7')}"}


def test_route_table():
    routes = build_routes(Settings())
    r, p = match(routes, "assistant/ask")
    assert r.service == "rag-assistant" and p == "/ask"
    r, p = match(routes, "products/42")
    assert r.service == "catalog-service" and p == "/products/42"
    assert match(routes, "unknown/1") is None


def test_login_is_public(client):
    r = client.post("/api/auth/login", json={"username": "a", "password": "b"})
    assert r.status_code == 200 and r.json()["path"] == "/auth/login"


def test_protected_route_requires_token(client):
    r = client.get("/api/products")
    assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized"
    assert not seen  # до сервиса запрос не дошёл


def test_forwarding_with_token_and_query(client):
    r = client.get("/api/products?q=box", headers={**bearer(), "X-User-Roles": "admin"})
    assert r.status_code == 200
    assert r.json() == {"host": "catalog", "path": "/products", "query": "q=box"}
    fwd = seen[-1]
    assert fwd.headers["x-user-id"] == "7"
    assert fwd.headers["x-user-roles"] == "manager"  # подделанный заголовок клиента заменён
    assert fwd.headers["authorization"].startswith("Bearer ")
    assert fwd.headers["x-request-id"] == r.headers["x-request-id"]


def test_assistant_prefix_is_stripped(client):
    r = client.post("/api/assistant/ask", json={"question": "x"}, headers=bearer(["picker"]))
    assert r.json()["host"] == "rag" and r.json()["path"] == "/ask"


def test_upstream_timeout_and_unavailable(client):
    r = client.get("/api/orders", headers=bearer())
    assert r.status_code == 504 and r.json()["error"]["code"] == "upstream_timeout"
    r = client.get("/api/stock", headers=bearer())
    assert r.status_code == 503 and r.json()["error"]["code"] == "upstream_unavailable"


def test_unknown_route_404(client):
    assert client.get("/api/nothing", headers=bearer()).status_code == 404


def test_expired_and_foreign_tokens_rejected(client):
    expired = make_token(PRIV, ["admin"], ttl=-10)
    assert client.get("/api/products", headers={"Authorization": f"Bearer {expired}"}).json()["error"]["code"] == "token_expired"
    other_priv, _ = generate_keypair()
    forged = make_token(other_priv, ["admin"])
    assert client.get("/api/products", headers={"Authorization": f"Bearer {forged}"}).status_code == 401


def test_rate_limit():
    seen.clear()
    with make(rate_limit_per_minute=3) as c:
        codes = [c.get("/api/products", headers=bearer()).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[3] == 429
