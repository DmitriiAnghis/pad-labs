"""Тесты общего пакета: формат логов, перевод ответов другого сервиса в ошибки, идемпотентность событий."""
import json
import logging

import httpx
import pytest
from sqlalchemy import select

from pad_common.db import Base, Database
from pad_common.errors import AppError, UpstreamError, UpstreamTimeout
from pad_common.events import EventEnvelope, OutboxMessage, ProcessedEvent, add_outbox, handle_once
from pad_common.http import ServiceClient
from pad_common.logging import JsonFormatter, request_id_var, setup_logging


def test_json_log_has_required_fields():
    setup_logging("test-service", "INFO", "json")
    token = request_id_var.set("abc123")
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    record.extra_fields = {"order_id": "o1"}
    line = json.loads(JsonFormatter().format(record))
    request_id_var.reset(token)
    assert {"timestamp", "service", "level", "request_id", "message"} <= line.keys()
    assert (line["service"], line["request_id"], line["order_id"]) == ("test-service", "abc123", "o1")


def client_for(status=None, exc=None, body=None):
    def handler(request):
        if exc:
            raise exc("boom", request=request)
        return httpx.Response(status, json=body or {})
    return ServiceClient("other", "http://other", transport=httpx.MockTransport(handler))


def test_service_client_error_mapping():
    with pytest.raises(UpstreamTimeout):
        client_for(exc=httpx.ReadTimeout).get("/x")
    with pytest.raises(UpstreamError) as e:
        client_for(exc=httpx.ConnectError).get("/x")
    assert e.value.status == 503
    with pytest.raises(UpstreamError):
        client_for(500).get("/x")
    with pytest.raises(AppError) as e:
        client_for(409, body={"error": {"code": "insufficient_stock", "message": "no", "details": [1]}}).get("/x")
    assert (e.value.status, e.value.code, e.value.details) == (409, "insufficient_stock", [1])
    assert client_for(200, body={"ok": True}).get("/x") == {"ok": True}


def test_outbox_and_idempotent_handler():
    db = Database("sqlite://", Base.metadata)
    db.create_all()
    with db.session() as s:
        env = add_outbox(s, "svc", "thing.happened", {"n": 1})
    with db.session() as s:
        assert s.scalar(select(OutboxMessage)).event_id == env.event_id
    calls = []
    e = EventEnvelope(type="thing.happened", source="svc", payload={"n": 1})
    assert handle_once(db, e, lambda s, ev: calls.append(ev.event_id))
    assert not handle_once(db, e, lambda s, ev: calls.append(ev.event_id))
    assert calls == [e.event_id]


def test_failed_handler_does_not_mark_event_processed():
    db = Database("sqlite://", Base.metadata)
    db.create_all()
    e = EventEnvelope(type="t", source="s", payload={})

    def broken(s, ev):
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        handle_once(db, e, broken)
    with db.session() as s:
        assert s.get(ProcessedEvent, e.event_id) is None  # повтор обработает событие заново


def test_ready_is_degraded_but_200_without_rabbitmq(tmp_path):
    """Брокер недоступен: /ready = 200 со статусом degraded, Kubernetes не убирает сервис из Service.
    База недоступна по-прежнему даёт 503 (проверяется в тестах сервисов)."""
    from fastapi.testclient import TestClient

    from pad_common.app import create_service
    from pad_common.config import BaseServiceSettings

    settings = BaseServiceSettings(database_url=f"sqlite:///{tmp_path}/t.db",
                                   rabbitmq_url="amqp://guest:guest@127.0.0.1:1/")  # порт 1: соединения не будет
    with TestClient(create_service(settings, title="t", description="t")) as client:
        r = client.get("/ready")
    body = r.json()
    assert r.status_code == 200
    assert body["status"] == "degraded"
    assert body["checks"]["rabbitmq"] == {"ok": False, "required": False,
                                          "detail": "not connected, events wait in outbox"}
