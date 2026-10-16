"""API contract with a fake pipeline (no index, no LLM)."""
import pytest
from fastapi.testclient import TestClient

from api import app as api_module
from generation.ollama_client import OllamaError
from pipeline.rag import QueryError


class FakeAnswer:
    refused = False
    refusal_reason = None
    sources = []
    model = "fake"
    timings_ms = {"total": 1.0}

    def to_dict(self, debug=False):
        return {"query": "q", "answer": "Tick the box [1].", "refused": False, "refusal_reason": None, "language": "en",
                "sources": [{"n": 1, "cited": True, "title": "T", "section": "T > S", "url": "https://x", "doc_id": "d",
                             "chunk_id": "c", "score": 0.5}], "model": "fake", "timings_ms": {"total": 1.0}}


class FakePipeline:
    class config:
        class generation:
            backend = "extractive"

        class retrieval:
            translate_query = False

        class embedding:
            backend = "minilm"

    class index:
        key = "fake-index"
        chunks = [1, 2, 3]
        meta = {}

    def answer(self, q):
        if q == "boom":
            raise RuntimeError("secret internal detail")
        if q == "llm":
            raise OllamaError("Ollama is not reachable")
        if q.strip() == "":
            raise QueryError("empty question")
        return FakeAnswer()


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(api_module.state, "load", lambda: None)  # do not load the real index
    with TestClient(api_module.app) as c:
        yield c


def test_health_is_independent_from_pipeline(client):
    api_module.state.pipeline = None
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.headers["X-Request-ID"]
    assert client.get("/ready").status_code == 503
    assert client.post("/ask", json={"question": "hi"}).status_code == 503


def test_ask_and_errors(client):
    api_module.state.pipeline = FakePipeline()
    assert client.get("/ready").status_code == 200
    r = client.post("/ask", json={"question": "How?"}, headers={"X-Request-ID": "req-1"})
    assert r.status_code == 200 and r.json()["sources"][0]["cited"] is True
    assert r.headers["X-Request-ID"] == "req-1"

    r = client.post("/ask", json={"question": "boom"})
    assert r.status_code == 500 and "secret" not in r.text and r.json()["error"] == "internal error"

    r = client.post("/ask", json={"question": "llm"})
    assert r.status_code == 503 and "not reachable" in r.json()["error"]

    r = client.post("/ask", json={"question": "  "})
    assert r.status_code == 422 and r.json()["error"] == "empty question"

    r = client.post("/ask", json={})
    assert r.status_code == 422 and r.json()["error"].startswith("invalid request")


def test_ui_page(client):
    r = client.get("/")
    assert r.status_code == 200 and "<textarea" in r.text
