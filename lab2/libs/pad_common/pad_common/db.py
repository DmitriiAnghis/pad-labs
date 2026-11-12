"""Подключение к базе через SQLAlchemy.

Берёт: DATABASE_URL (PostgreSQL в Docker, SQLite в unit-тестах).
Делает: создаёт engine (пул соединений) и фабрику сессий; при старте создаёт таблицы.
Получается: Database с методом session() для эндпоинтов и обработчиков событий.
Зачем: у каждого сервиса своя база; другие сервисы к ней не подключаются, только через API.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Базовый класс таблиц. Каждый сервис объявляет своих наследников в своём models.py."""


class Database:
    def __init__(self, url: str, metadata) -> None:
        kwargs: dict = {"pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs = {"connect_args": {"check_same_thread": False}}
            if url in ("sqlite://", "sqlite:///:memory:"):
                kwargs["poolclass"] = StaticPool
        self.url = url
        self.engine = create_engine(url, **kwargs)
        self.metadata = metadata
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    @property
    def is_postgres(self) -> bool:
        return self.engine.dialect.name == "postgresql"

    def create_all(self) -> None:
        self.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Одна транзакция: commit, если блок прошёл без ошибок, иначе rollback."""
        s = self._sessions()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def ping(self) -> tuple[bool, str]:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return True, self.engine.dialect.name
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}"
