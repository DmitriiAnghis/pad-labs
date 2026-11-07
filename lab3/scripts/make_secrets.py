"""Создать lab3/k8s/secrets/secrets.env для Kubernetes Secret pad-secrets.

Берёт: lab2/.env (его создаёт lab2/scripts/init_env.py: случайные пароли и ключ JWT).
       Если lab2/.env ещё нет, сначала запускает init_env.py.
Делает: оставляет только секретные ключи (пароли, ключ JWT) и проверяет, что ни один не пуст
        и не равен change-me.
Получается: файл lab3/k8s/secrets/secrets.env (в Git не попадает). Из него kubectl apply -k
            создаёт Secret pad-secrets (secretGenerator в kustomization.yaml).
Зачем: одни и те же пароли в docker compose и в Kubernetes, без ручного копирования.
Запуск из корня репозитория:  python lab3/scripts/make_secrets.py   (--force перезаписать)
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
LAB2_ENV = REPO / "lab2" / ".env"
TARGET = REPO / "lab3" / "k8s" / "secrets" / "secrets.env"
KEYS = [
    "POSTGRES_PASSWORD", "AUTH_DB_PASSWORD", "CATALOG_DB_PASSWORD", "INVENTORY_DB_PASSWORD",
    "ORDER_DB_PASSWORD", "FULFILLMENT_DB_PASSWORD", "NOTIFICATION_DB_PASSWORD",
    "RABBITMQ_USER", "RABBITMQ_PASSWORD", "ADMIN_PASSWORD", "JWT_PRIVATE_KEY",
]


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def main() -> int:
    if TARGET.exists() and "--force" not in sys.argv:
        print(f"{TARGET} already exists, use --force to overwrite")
        return 0
    if not LAB2_ENV.exists():
        print("lab2/.env not found, running lab2/scripts/init_env.py")
        subprocess.run([sys.executable, str(REPO / "lab2" / "scripts" / "init_env.py")], check=True)
    env = read_env(LAB2_ENV)
    bad = [k for k in KEYS if not env.get(k) or env[k] == "change-me"]
    if bad:
        print("empty or change-me in lab2/.env: " + ", ".join(bad))
        if "JWT_PRIVATE_KEY" in bad:
            print("JWT_PRIVATE_KEY is required in Kubernetes: pip install cryptography, "
                  "then python lab2/scripts/init_env.py --force")
        return 1
    lines = ["# Создано lab3/scripts/make_secrets.py из lab2/.env. Не коммитить."]
    lines += [f"{k}={env[k]}" for k in KEYS]
    TARGET.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"created {TARGET} ({len(KEYS)} keys)\nadmin login: admin / {env['ADMIN_PASSWORD']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
