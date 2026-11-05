"""Тесты auth-service: вход, роли, refresh, отключение пользователя."""
import pytest
from fastapi.testclient import TestClient

from auth_service.config import Settings
from auth_service.main import build_app


@pytest.fixture()
def client():
    settings = Settings(database_url="sqlite://", admin_password="admin-pass-1", log_format="text")
    with TestClient(build_app(settings)) as c:
        yield c


def login(c, username="admin", password="admin-pass-1"):
    r = c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def auth(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def test_health_and_ready(client):
    assert client.get("/health").json()["status"] == "ok"
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"]["ok"]


def test_login_wrong_password_returns_401_without_stack_trace(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "nope"})
    assert r.status_code == 401
    body = r.json()
    assert body["error"]["code"] == "invalid_credentials"
    assert "request_id" in body and "Traceback" not in r.text


def test_admin_creates_user_and_roles_are_enforced(client):
    admin = login(client)
    r = client.post("/users", headers=auth(admin),
                    json={"username": "picker1", "password": "picker-pass", "roles": ["picker"]})
    assert r.status_code == 201, r.text
    picker = login(client, "picker1", "picker-pass")
    assert client.get("/auth/me", headers=auth(picker)).json()["roles"] == ["picker"]
    # picker не может управлять пользователями
    r = client.get("/users", headers=auth(picker))
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    # дубликат логина
    r = client.post("/users", headers=auth(admin), json={"username": "picker1", "password": "x" * 8, "roles": ["picker"]})
    assert r.status_code == 409


def test_validation_error_format(client):
    admin = login(client)
    r = client.post("/users", headers=auth(admin), json={"username": "a", "password": "short", "roles": ["boss"]})
    assert r.status_code == 422
    fields = {d["field"] for d in r.json()["error"]["details"]}
    assert {"username", "password"} <= fields


def test_no_token_is_401(client):
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401


def test_refresh_is_single_use_and_logout_revokes(client):
    t = login(client)
    r = client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]})
    assert r.status_code == 200
    # тот же refresh второй раз не работает
    assert client.post("/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    new = r.json()
    assert client.post("/auth/logout", json={"refresh_token": new["refresh_token"]}).status_code == 204
    assert client.post("/auth/refresh", json={"refresh_token": new["refresh_token"]}).status_code == 401


def test_access_token_cannot_be_used_as_refresh(client):
    t = login(client)
    assert client.post("/auth/refresh", json={"refresh_token": t["access_token"]}).status_code == 401


def test_disabled_user_cannot_login(client):
    admin = login(client)
    uid = client.post("/users", headers=auth(admin),
                      json={"username": "mgr", "password": "manager-pass", "roles": ["manager"]}).json()["id"]
    client.patch(f"/users/{uid}", headers=auth(admin), json={"is_active": False})
    r = client.post("/auth/login", json={"username": "mgr", "password": "manager-pass"})
    assert r.status_code == 401


def test_public_key_endpoint(client):
    body = client.get("/auth/public-key").json()
    assert body["algorithm"] == "RS256" and "BEGIN PUBLIC KEY" in body["public_key"]
