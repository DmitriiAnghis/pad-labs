"""Пароли и токены.

Пароль хранится только как bcrypt-хэш: из хэша пароль не восстановить, при входе хэшируется
введённый пароль и сравнивается. Access-токен (JWT, 15 минут) подписывается закрытым ключом
RSA; остальные сервисы проверяют подпись открытым ключом и не обращаются к auth-service.
Refresh-токен (7 дней) тоже JWT, но его id хранится в базе, чтобы выход из системы мог его отозвать.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from cryptography.hazmat.primitives import serialization

from pad_common.testing import generate_keypair

log = logging.getLogger("security")


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def check_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


class KeyPair:
    def __init__(self, private_pem: str) -> None:
        if private_pem:
            private_pem = private_pem.replace("\\n", "\n")
            key = serialization.load_pem_private_key(private_pem.encode(), password=None)
            public_pem = key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo).decode()
            self.generated = False
        else:
            private_pem, public_pem = generate_keypair()
            self.generated = True
            log.warning("JWT_PRIVATE_KEY is empty: generated a temporary key pair (development only)")
        self.private_pem = private_pem
        self.public_pem = public_pem


class TokenIssuer:
    def __init__(self, keys: KeyPair, issuer: str, audience: str, access_ttl: int, refresh_ttl: int) -> None:
        self.keys, self.issuer, self.audience = keys, issuer, audience
        self.access_ttl, self.refresh_ttl = access_ttl, refresh_ttl

    def _encode(self, claims: dict, ttl: int) -> str:
        now = int(time.time())
        claims = {**claims, "iat": now, "exp": now + ttl, "iss": self.issuer, "aud": self.audience}
        return jwt.encode(claims, self.keys.private_pem, algorithm="RS256")

    def access_token(self, user_id: int, username: str, roles: list[str]) -> str:
        return self._encode({"sub": str(user_id), "username": username, "roles": roles, "typ": "access",
                             "jti": uuid.uuid4().hex}, self.access_ttl)

    def refresh_token(self, user_id: int) -> tuple[str, str, datetime]:
        jti = uuid.uuid4().hex
        token = self._encode({"sub": str(user_id), "typ": "refresh", "jti": jti}, self.refresh_ttl)
        return token, jti, datetime.now(timezone.utc) + timedelta(seconds=self.refresh_ttl)
