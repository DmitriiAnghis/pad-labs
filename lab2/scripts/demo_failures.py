"""Демонстрация отказов для защиты: «что будет, если сервис упал».

Берёт: запущенную систему (docker compose up) и .env (пароли admin и RabbitMQ).
Делает три опыта, каждый раз останавливая один контейнер через docker compose:
  1. notification-service выключен -> заказы создаются, события копятся в очереди RabbitMQ,
     после запуска сервиса уведомление появляется;
  2. inventory-service выключен -> создание заказа получает понятный 503 без stack trace;
  3. RabbitMQ выключен -> заказ всё равно создаётся (событие ждёт в таблице outbox),
     после запуска брокера задание на сборку появляется само.
Получается: таблица результатов в консоли (её можно вставить в README).
Запуск из папки lab2:  python scripts/demo_failures.py
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://localhost:8080/api"
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text(encoding="utf-8").splitlines() if "=" in l and not l.startswith("#"))
RUN = uuid.uuid4().hex[:5].upper()


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True)


def wait(fn, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = fn()
            if v:
                return v
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return None


def get_list(url, h):
    r = httpx.get(url, headers=h)
    return r.json() if r.status_code == 200 else []


def token(user, password):
    return httpx.post(f"{BASE}/auth/login", json={"username": user, "password": password}).json()["access_token"]


def main() -> int:
    adm = {"Authorization": f"Bearer {token('admin', ENV['ADMIN_PASSWORD'])}"}
    mgr_name = f"demo-{RUN}"
    httpx.post(f"{BASE}/users", headers=adm, json={"username": mgr_name, "password": "password-123", "roles": ["manager"]})
    h = {"Authorization": f"Bearer {token(mgr_name, 'password-123')}"}
    pid = httpx.post(f"{BASE}/products", headers=h, json={"sku": f"DEMO-{RUN}", "name": "demo"}).json()["id"]
    httpx.post(f"{BASE}/locations", headers=h, json={"code": f"Z-{RUN}", "zone": "Z"})
    httpx.post(f"{BASE}/stock/receipts", headers=h, json={"product_id": pid, "location_code": f"Z-{RUN}", "quantity": 100})
    order_body = {"customer_name": "demo", "shipping_address": "-", "lines": [{"product_id": pid, "quantity": 1}]}
    rows = []

    def queue_depth(name):
        r = httpx.get(f"http://localhost:15672/api/queues/%2F/{name}", auth=(ENV["RABBITMQ_USER"], ENV["RABBITMQ_PASSWORD"]))
        return r.json().get("messages", 0)

    # 1. notification-service выключен
    compose("stop", "notification-service")
    o = httpx.post(f"{BASE}/orders", headers=h, json=order_body)
    depth = wait(lambda: queue_depth("notification-service"), 10) or 0
    notif_down = httpx.get(f"{BASE}/notifications", headers=h).status_code
    compose("start", "notification-service")
    got = wait(lambda: any(o.json()["number"] in n["title"] for n in get_list(f"{BASE}/notifications?limit=500", h)))
    rows.append(("notification-service", f"заказ {o.status_code}; GET /notifications {notif_down}; в очереди {depth} сообщ.",
                 "уведомление пришло после запуска" if got else "уведомление НЕ пришло"))

    # 2. inventory-service выключен
    compose("stop", "inventory-service")
    r = httpx.post(f"{BASE}/orders", headers=h, json=order_body)
    compose("start", "inventory-service")
    wait(lambda: httpx.get("http://localhost:8003/ready").status_code == 200)
    rows.append(("inventory-service", f"заказ {r.status_code} {r.json()['error']['code']}", "заказ не создан, товар не завис"))

    # 3. RabbitMQ выключен
    compose("stop", "rabbitmq")
    r = httpx.post(f"{BASE}/orders", headers=h, json=order_body)
    oid = r.json().get("id")
    compose("start", "rabbitmq")
    picked = wait(lambda: any(p["order_id"] == oid for p in get_list(f"{BASE}/pickings?limit=500", h)), 90)
    rows.append(("rabbitmq", f"заказ {r.status_code} (событие в outbox)",
                 "задание на сборку появилось после запуска брокера" if picked else "задание НЕ появилось"))

    print(f"\n{'Упал':<22} | {'Во время отказа':<55} | После восстановления")
    print("-" * 120)
    for name, during, after in rows:
        print(f"{name:<22} | {during:<55} | {after}")
    return 0 if all("НЕ" not in r[2] for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
