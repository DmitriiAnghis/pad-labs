"""Создать lab2/.env из .env.example: случайные пароли и ключ подписи JWT.

Берёт: .env.example (шаблон с change-me).
Делает: заменяет каждое change-me на случайную строку; генерирует пару ключей RSA 2048
и записывает закрытый ключ в JWT_PRIVATE_KEY.
Получается: файл lab2/.env (в Git не попадает). Существующий .env не перезаписывается.
Запуск: python scripts/init_env.py   (нужен пакет cryptography: pip install cryptography)
"""
from __future__ import annotations

import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    target, template = ROOT / ".env", ROOT / ".env.example"
    if target.exists() and "--force" not in sys.argv:
        print(f"{target} already exists, use --force to overwrite")
        return 1
    lines = []
    for line in template.read_text(encoding="utf-8").splitlines():
        if line.endswith("=change-me"):
            line = line.replace("change-me", secrets.token_urlsafe(18))
        elif line.startswith("JWT_PRIVATE_KEY="):
            try:
                from cryptography.hazmat.primitives import serialization
                from cryptography.hazmat.primitives.asymmetric import rsa

                key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
                pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()
                line = "JWT_PRIVATE_KEY=" + pem.strip().replace("\n", "\\n")
            except ImportError:
                print("cryptography is not installed: JWT_PRIVATE_KEY left empty (auth-service will generate a temporary key)")
        lines.append(line)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    admin = next(l.split("=", 1)[1] for l in lines if l.startswith("ADMIN_PASSWORD="))
    print(f"created {target}\nadmin login: admin / {admin}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
