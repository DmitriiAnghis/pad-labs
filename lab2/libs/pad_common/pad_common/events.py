"""События между сервисами через RabbitMQ.

Как это устроено (по шагам):
1. Сервис меняет свои таблицы и в той же транзакции кладёт событие в таблицу outbox
   (функция add_outbox). Если транзакция откатилась, события нет; если прошла, событие точно
   сохранено, даже если RabbitMQ сейчас лежит. Это шаблон Transactional Outbox.
2. Фоновая задача OutboxRelay раз в полсекунды берёт неотправленные строки outbox и публикует
   их в exchange warehouse.events (тип topic) с routing key = тип события (order.confirmed).
3. Сервис-получатель объявляет свою очередь (например notification-service) и привязывает её
   к нужным типам событий. Обработчик выполняется в транзакции вместе с записью event_id в
   таблицу processed_events: если брокер доставит то же событие второй раз, оно будет пропущено
   (идемпотентность).
4. Если обработчик упал, событие повторяется до 3 раз с паузой; потом уходит в очередь
   <service>.dlq (dead letter queue), где его можно посмотреть в панели RabbitMQ.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, Field
from sqlalchemy import JSON, DateTime, Integer, String, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from .db import Base, Database
from .logging import log_event, request_id_var

log = logging.getLogger("events")

MAX_RETRIES = 3
DLX = "warehouse.dlx"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class EventEnvelope(BaseModel):
    """Одинаковая «обёртка» любого события."""
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    type: str
    source: str
    occurred_at: datetime = Field(default_factory=utcnow)
    request_id: str = "-"
    payload: dict[str, Any]


class OutboxMessage(Base):
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True)
    type: Mapped[str] = mapped_column(String(64), index=True)
    body: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    event_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def add_outbox(session: Session, source: str, event_type: str, payload: dict) -> EventEnvelope:
    """Положить событие в outbox в текущей транзакции. Отправит его OutboxRelay."""
    env = EventEnvelope(type=event_type, source=source, request_id=request_id_var.get(), payload=payload)
    session.add(OutboxMessage(event_id=env.event_id, type=event_type, body=json.loads(env.model_dump_json())))
    return env


Handler = Callable[[Session, EventEnvelope], None]


@dataclass
class Subscription:
    event_type: str
    handler: Handler


def handle_once(db: Database, env: EventEnvelope, handler: Handler) -> bool:
    """Выполнить обработчик ровно один раз для данного event_id. True = обработано сейчас."""
    try:
        with db.session() as s:
            s.add(ProcessedEvent(event_id=env.event_id, type=env.type))
            s.flush()  # если такой event_id уже есть, здесь будет IntegrityError
            handler(s, env)
        return True
    except IntegrityError:
        log_event(log, "duplicate event skipped", event_id=env.event_id, type=env.type)
        return False


class EventBus:
    """Соединение с RabbitMQ: публикация из outbox и приём событий."""

    def __init__(self, url: str, exchange: str, service: str, db: Database) -> None:
        self.url = url
        self.exchange_name = exchange
        self.service = service
        self.db = db
        self.subscriptions: list[Subscription] = []
        self.connection = None
        self.channel = None
        self.exchange = None
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self.subscriptions.append(Subscription(event_type, handler))

    def is_connected(self) -> bool:
        """Есть ли сейчас живое соединение с RabbitMQ.

        connect_robust при обрыве сам переподключается, и is_closed у него остаётся False, пока
        не вызван close(). Поэтому дополнительно смотрим transport: во время переподключения
        он None (проверено в лабе 3: брокер остановлен, а сервис продолжал считать себя подключённым).
        """
        c = self.connection
        return c is not None and not c.is_closed and getattr(c, "transport", None) is not None

    async def start(self) -> None:
        if not self.enabled:
            log.info("RABBITMQ_URL is empty: events are disabled")
            return
        self._tasks.append(asyncio.create_task(self._run(), name="event-bus"))

    async def stop(self) -> None:
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        if self.connection is not None:
            await self.connection.close()

    async def _run(self) -> None:
        import aio_pika

        delay = 1.0
        while not self._stopping.is_set():
            try:
                self.connection = await aio_pika.connect_robust(self.url)
                self.channel = await self.connection.channel()
                await self.channel.set_qos(prefetch_count=10)
                self.exchange = await self.channel.declare_exchange(self.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True)
                if self.subscriptions:
                    await self._declare_consumer(aio_pika)
                log_event(log, "connected to RabbitMQ", subscriptions=[s.event_type for s in self.subscriptions])
                break
            except Exception as exc:  # noqa: BLE001
                log_event(log, "RabbitMQ connection failed, retrying", logging.WARNING, error=type(exc).__name__, retry_in_s=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 15)
        await self._relay_loop()

    async def _declare_consumer(self, aio_pika) -> None:
        dlx = await self.channel.declare_exchange(DLX, aio_pika.ExchangeType.DIRECT, durable=True)
        dlq = await self.channel.declare_queue(f"{self.service}.dlq", durable=True)
        await dlq.bind(dlx, routing_key=self.service)
        queue = await self.channel.declare_queue(
            self.service, durable=True,
            arguments={"x-dead-letter-exchange": DLX, "x-dead-letter-routing-key": self.service})
        for sub in self.subscriptions:
            await queue.bind(self.exchange, routing_key=sub.event_type)
        await queue.consume(self._on_message)

    async def _on_message(self, message) -> None:
        import aio_pika

        try:
            env = EventEnvelope.model_validate_json(message.body)
        except Exception:  # noqa: BLE001
            log.error("malformed event, sent to DLQ")
            await message.reject(requeue=False)
            return
        token = request_id_var.set(env.request_id)
        try:
            handlers = [s.handler for s in self.subscriptions if s.event_type == env.type]
            for h in handlers:
                done = await asyncio.to_thread(handle_once, self.db, env, h)
                if done:
                    log_event(log, "event processed", event_id=env.event_id, type=env.type, source=env.source)
            await message.ack()
        except Exception as exc:  # noqa: BLE001
            retries = int((message.headers or {}).get("x-retries", 0))
            if retries < MAX_RETRIES:
                log_event(log, "event handler failed, will retry", logging.WARNING, event_id=env.event_id,
                          type=env.type, retry=retries + 1, error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(0.5 * 2 ** retries)
                await self.channel.default_exchange.publish(
                    aio_pika.Message(message.body, headers={"x-retries": retries + 1}, delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                                     content_type="application/json"),
                    routing_key=self.service)
                await message.ack()
            else:
                log.exception("event handler failed %s times, sent to DLQ: %s", MAX_RETRIES, env.event_id)
                await message.reject(requeue=False)
        finally:
            request_id_var.reset(token)

    async def _relay_loop(self) -> None:
        """OutboxRelay: переносит события из таблицы outbox в RabbitMQ."""
        while not self._stopping.is_set():
            try:
                sent = await self.publish_pending()
                await asyncio.sleep(0.05 if sent else 0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log_event(log, "outbox relay error", logging.WARNING, error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(2)

    async def publish_pending(self, limit: int = 50) -> int:
        """Гарантия «хотя бы один раз»: если сервис упадёт между публикацией и отметкой
        published_at, событие уйдёт повторно. Это безопасно, потому что получатели
        пропускают уже обработанные event_id. По той же причине можно запускать несколько
        копий сервиса: редкий дубль отсеется на стороне получателя."""
        import aio_pika

        if self.exchange is None or not self.is_connected():
            return 0

        def take() -> list[OutboxMessage]:
            with self.db.session() as s:
                q = select(OutboxMessage).where(OutboxMessage.published_at.is_(None)).order_by(OutboxMessage.id).limit(limit)
                return list(s.scalars(q))

        rows = await asyncio.to_thread(take)
        for row in rows:
            await self.exchange.publish(
                aio_pika.Message(json.dumps(row.body).encode(), message_id=row.event_id, type=row.type,
                                 content_type="application/json", delivery_mode=aio_pika.DeliveryMode.PERSISTENT),
                routing_key=row.type)

            def mark(rid=row.id) -> None:
                with self.db.session() as s:
                    msg = s.get(OutboxMessage, rid)
                    if msg is not None:
                        msg.published_at = utcnow()

            await asyncio.to_thread(mark)
            log_event(log, "event published", event_id=row.event_id, type=row.type)
        return len(rows)
