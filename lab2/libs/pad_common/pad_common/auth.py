"""Проверка JWT и ролей внутри сервиса.

Берёт: заголовок Authorization: Bearer <JWT>, который пришёл от gateway или от другого сервиса.
Делает: проверяет подпись открытым ключом auth-service (алгоритм RS256), срок действия,
издателя (iss) и получателя (aud); достаёт id пользователя и роли.
Получается: объект Principal, который эндпоинт получает через Depends(require_roles(...)).
Зачем: gateway тоже проверяет токен, но сервис не должен верить запросу только потому, что тот
дошёл до него. Если кто-то обратится к сервису в обход gateway, без токена он получит 401.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import httpx
import jwt
from fastapi import Request

from .config import BaseServiceSettings
from .errors import Forbidden, Unauthorized, UpstreamError

log = logging.getLogger("auth")

ROLES = ("admin", "manager", "picker")


@dataclass
class Principal:
    user_id: str
    username: str
    roles: list[str] = field(default_factory=list)
    token: str = ""

    def has_any(self, *roles: str) -> bool:
        return "admin" in self.roles or any(r in self.roles for r in roles)


class PublicKeyProvider:
    """Открытый ключ: из настроек или один раз скачивается у auth-service и кэшируется."""

    def __init__(self, settings: BaseServiceSettings) -> None:
        self.settings = settings
        self._key: str | None = settings.jwt_public_key.replace("\\n", "\n") or None
        self._lock = threading.Lock()

    def get(self) -> str:
        if self._key:
            return self._key
        with self._lock:
            if not self._key:
                try:
                    r = httpx.get(self.settings.auth_public_key_url, timeout=self.settings.http_timeout)
                    r.raise_for_status()
                    self._key = r.json()["public_key"]
                    log.info("public key loaded from auth-service")
                except Exception as exc:  # noqa: BLE001
                    raise UpstreamError(f"cannot load JWT public key from auth-service: {type(exc).__name__}") from exc
        return self._key

    def loaded(self) -> bool:
        return self._key is not None

    def reset(self) -> None:
        self._key = self.settings.jwt_public_key.replace("\\n", "\n") or None


def decode_token(token: str, public_key: str, issuer: str, audience: str) -> dict:
    try:
        return jwt.decode(token, public_key, algorithms=["RS256"], issuer=issuer, audience=audience,
                          options={"require": ["exp", "sub", "iss", "aud"]})
    except jwt.ExpiredSignatureError as exc:
        raise Unauthorized("token expired", code="token_expired") from exc
    except jwt.PyJWTError as exc:
        raise Unauthorized(f"invalid token: {exc}", code="invalid_token") from exc


def bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise Unauthorized("missing bearer token")
    return token.strip()


def principal_from_request(request: Request) -> Principal:
    provider: PublicKeyProvider = request.app.state.key_provider
    settings: BaseServiceSettings = request.app.state.settings
    token = bearer_token(request)
    claims = decode_token(token, provider.get(), settings.jwt_issuer, settings.jwt_audience)
    if claims.get("typ", "access") != "access":
        raise Unauthorized("not an access token", code="invalid_token")
    return Principal(user_id=str(claims["sub"]), username=claims.get("username", ""),
                     roles=list(claims.get("roles", [])), token=token)


def require_roles(*roles: str):
    """Зависимость FastAPI: пускает пользователя с любой из ролей; admin пускается всегда.

    require_roles() без аргументов = достаточно быть вошедшим в систему.
    """

    def dependency(request: Request) -> Principal:
        principal = principal_from_request(request)
        if roles and not principal.has_any(*roles):
            raise Forbidden(f"requires one of roles: {', '.join(roles)}")
        return principal

    return dependency
