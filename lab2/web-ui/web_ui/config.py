"""Настройки web-ui из переменных окружения.

Берёт: переменные окружения (в Docker Compose блок environment, в Kubernetes ConfigMap).
Делает: проверяет типы, подставляет значения по умолчанию.
Получается: объект Settings, который остальные части сервиса получают через DI (deps.py).
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "web-ui"
    log_level: str = "INFO"
    log_format: str = "json"

    # Адрес API Gateway. web-ui ходит в систему только через него, как любой другой клиент.
    gateway_url: str = "http://gateway:8000"
    # Таймаут запроса к gateway, секунды.
    http_timeout: float = 5.0
    # Флаг Secure у cookie с токеном: true, только если сайт открыт по HTTPS.
    cookie_secure: bool = False
