"""Помощники для тестов: пара RSA-ключей и выпуск JWT без auth-service."""
from __future__ import annotations

import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate_keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


def make_token(private_key: str, roles: list[str], user_id: str = "1", username: str = "tester",
               ttl: int = 600, issuer: str = "pad-auth", audience: str = "pad-warehouse") -> str:
    now = int(time.time())
    return jwt.encode({"sub": user_id, "username": username, "roles": roles, "typ": "access", "iat": now,
                       "exp": now + ttl, "iss": issuer, "aud": audience, "jti": uuid.uuid4().hex},
                      private_key, algorithm="RS256")
