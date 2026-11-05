"""Эндпоинты auth-service.

POST /auth/login        логин + пароль -> access и refresh токены
POST /auth/refresh      refresh -> новая пара токенов (старый refresh отзывается)
POST /auth/logout       отозвать refresh-токен
GET  /auth/me           кто я (по access-токену)
GET  /auth/public-key   открытый ключ для проверки JWT (нужен gateway и сервисам)
GET  /users             список пользователей (admin)
POST /users             создать пользователя (admin)
PATCH /users/{id}       изменить роли, имя, пароль, активность (admin)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

import jwt
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from pad_common.app import get_session
from pad_common.auth import Principal, require_roles
from pad_common.errors import Conflict, NotFound, Unauthorized
from pad_common.logging import log_event

from .models import RefreshToken, User
from .security import TokenIssuer, check_password, hash_password

import logging

log = logging.getLogger("auth")
router = APIRouter()

Role = Literal["admin", "manager", "picker"]


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64, examples=["admin"])
    password: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class UserOut(BaseModel):
    id: int
    username: str
    full_name: str
    roles: list[str]
    is_active: bool


class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$", examples=["ivan.picker"])
    password: str = Field(..., min_length=8, max_length=128)
    full_name: str = Field("", max_length=128)
    roles: list[Role] = Field(..., min_length=1, examples=[["picker"]])


class UserUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=128)
    password: str | None = Field(None, min_length=8, max_length=128)
    roles: list[Role] | None = Field(None, min_length=1)
    is_active: bool | None = None


def user_out(u: User) -> UserOut:
    return UserOut(id=u.id, username=u.username, full_name=u.full_name, roles=u.role_list, is_active=u.is_active)


def issuer(request: Request) -> TokenIssuer:
    return request.app.state.issuer


def _issue(s: Session, iss: TokenIssuer, user: User) -> TokenPair:
    refresh, jti, exp = iss.refresh_token(user.id)
    s.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp))
    return TokenPair(access_token=iss.access_token(user.id, user.username, user.role_list), refresh_token=refresh,
                     expires_in=iss.access_ttl)


@router.post("/auth/login", response_model=TokenPair, tags=["auth"], summary="Вход: логин и пароль -> токены")
def login(body: LoginRequest, s: Session = Depends(get_session), iss: TokenIssuer = Depends(issuer)):
    user = s.scalar(select(User).where(User.username == body.username))
    if user is None or not user.is_active or not check_password(body.password, user.password_hash):
        log_event(log, "login failed", logging.WARNING, username=body.username)
        raise Unauthorized("wrong username or password", code="invalid_credentials")
    log_event(log, "login ok", user_id=user.id, roles=user.roles)
    return _issue(s, iss, user)


@router.post("/auth/refresh", response_model=TokenPair, tags=["auth"], summary="Обновить токены по refresh-токену")
def refresh(body: RefreshRequest, s: Session = Depends(get_session), iss: TokenIssuer = Depends(issuer)):
    try:
        claims = jwt.decode(body.refresh_token, iss.keys.public_pem, algorithms=["RS256"], issuer=iss.issuer,
                            audience=iss.audience)
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid refresh token", code="invalid_token") from exc
    if claims.get("typ") != "refresh":
        raise Unauthorized("not a refresh token", code="invalid_token")
    stored = s.get(RefreshToken, claims["jti"])
    if stored is None or stored.revoked:
        raise Unauthorized("refresh token revoked", code="invalid_token")
    user = s.get(User, int(claims["sub"]))
    if user is None or not user.is_active:
        raise Unauthorized("user is disabled", code="invalid_token")
    stored.revoked = True  # каждый refresh-токен одноразовый
    return _issue(s, iss, user)


@router.post("/auth/logout", status_code=204, tags=["auth"], summary="Выход: отозвать refresh-токен")
def logout(body: RefreshRequest, s: Session = Depends(get_session), iss: TokenIssuer = Depends(issuer)):
    try:
        claims = jwt.decode(body.refresh_token, iss.keys.public_pem, algorithms=["RS256"], issuer=iss.issuer,
                            audience=iss.audience, options={"verify_exp": False})
    except jwt.PyJWTError as exc:
        raise Unauthorized("invalid refresh token", code="invalid_token") from exc
    stored = s.get(RefreshToken, claims.get("jti", ""))
    if stored is not None:
        stored.revoked = True


@router.get("/auth/me", response_model=UserOut, tags=["auth"], summary="Текущий пользователь")
def me(p: Principal = Depends(require_roles()), s: Session = Depends(get_session)):
    user = s.get(User, int(p.user_id))
    if user is None:
        raise NotFound("user not found")
    return user_out(user)


@router.get("/auth/public-key", tags=["auth"], summary="Открытый ключ RSA для проверки JWT")
def public_key(iss: TokenIssuer = Depends(issuer)):
    return {"algorithm": "RS256", "public_key": iss.keys.public_pem, "issuer": iss.issuer, "audience": iss.audience}


@router.get("/users", response_model=list[UserOut], tags=["users"], summary="Список пользователей (admin)")
def list_users(_: Principal = Depends(require_roles("admin")), s: Session = Depends(get_session)):
    return [user_out(u) for u in s.scalars(select(User).order_by(User.id))]


@router.post("/users", response_model=UserOut, status_code=201, tags=["users"], summary="Создать пользователя (admin)")
def create_user(body: UserCreate, p: Principal = Depends(require_roles("admin")), s: Session = Depends(get_session)):
    if s.scalar(select(User).where(User.username == body.username)):
        raise Conflict(f"user '{body.username}' already exists", code="user_exists")
    user = User(username=body.username, full_name=body.full_name, password_hash=hash_password(body.password),
                roles=",".join(sorted(set(body.roles))))
    s.add(user)
    s.flush()
    log_event(log, "user created", user_id=user.id, roles=user.roles, by=p.username)
    return user_out(user)


@router.patch("/users/{user_id}", response_model=UserOut, tags=["users"], summary="Изменить пользователя (admin)")
def update_user(user_id: int, body: UserUpdate, p: Principal = Depends(require_roles("admin")),
                s: Session = Depends(get_session)):
    user = s.get(User, user_id)
    if user is None:
        raise NotFound(f"user {user_id} not found")
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.password is not None:
        user.password_hash = hash_password(body.password)
    if body.roles is not None:
        user.roles = ",".join(sorted(set(body.roles)))
    if body.is_active is not None:
        user.is_active = body.is_active
        if not body.is_active:  # отключённый пользователь не может обновить токены
            for t in s.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))):
                t.revoked = True
    log_event(log, "user updated", user_id=user.id, by=p.username)
    return user_out(user)


