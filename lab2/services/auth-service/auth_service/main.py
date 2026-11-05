"""auth-service: пользователи, роли, выдача JWT.

Запуск: uvicorn auth_service.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import logging

from fastapi import FastAPI
from sqlalchemy import func, select

from pad_common.app import create_service
from pad_common.logging import log_event

from . import models  # noqa: F401 - регистрирует таблицы
from .api import router
from .config import Settings
from .security import KeyPair, TokenIssuer, hash_password

log = logging.getLogger("auth")


def bootstrap_admin(app: FastAPI) -> None:
    settings: Settings = app.state.settings
    with app.state.db.session() as s:
        if s.scalar(select(func.count()).select_from(models.User)):
            return
        if not settings.admin_password:
            log.warning("no users and ADMIN_PASSWORD is empty: nobody can log in")
            return
        s.add(models.User(username=settings.admin_username, full_name="Administrator",
                          password_hash=hash_password(settings.admin_password), roles="admin"))
        log_event(log, "bootstrap admin created", username=settings.admin_username)


def build_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    keys = KeyPair(settings.jwt_private_key)
    # auth-service проверяет свои же токены (для /auth/me и /users) тем же открытым ключом.
    settings.jwt_public_key = keys.public_pem
    app = create_service(settings, title="auth-service",
                         description="Пользователи, роли (admin, manager, picker) и выдача JWT (RS256).",
                         on_startup=bootstrap_admin)
    app.state.issuer = TokenIssuer(keys, settings.jwt_issuer, settings.jwt_audience,
                                   settings.access_token_ttl_s, settings.refresh_token_ttl_s)
    app.include_router(router)
    return app


app = build_app()
