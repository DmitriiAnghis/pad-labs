"""Настройки сервиса из переменных окружения.

Берёт: переменные окружения (в Docker Compose они приходят из .env и блока environment,
в Kubernetes из ConfigMap и Secret).
Делает: проверяет типы и подставляет значения по умолчанию.
Получается: объект BaseServiceSettings; каждый сервис наследует его и добавляет свои поля.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "service"
    port: int = 8000
    log_level: str = "INFO"
    log_format: str = "json"  # json для Docker/Kubernetes, text для чтения глазами

    # Строка подключения SQLAlchemy, например postgresql+psycopg://user:pass@postgres:5432/order_db.
    # Пароль внутри, поэтому в Kubernetes это Secret.
    database_url: str = "sqlite:///./service.db"

    # Адрес RabbitMQ; пустая строка = события отключены (так работают unit-тесты).
    rabbitmq_url: str = ""
    events_exchange: str = "warehouse.events"

    # Открытый ключ для проверки JWT. Либо сам ключ (PEM), либо адрес, откуда его взять у auth-service.
    jwt_public_key: str = ""
    auth_public_key_url: str = "http://auth-service:8000/auth/public-key"
    jwt_issuer: str = "pad-auth"
    jwt_audience: str = "pad-warehouse"

    # Таймаут запросов к другим сервисам, секунды.
    http_timeout: float = 2.0
