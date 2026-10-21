"""API contract with a fake pipeline (no index, no LLM)."""
import pytest
from fastapi.testclient import TestClient

from api import app as api_module
from generation.ollama_client import OllamaError
from common.config import PipelineConfig
from history.store import AnswerHistory
from pipeline.rag import QueryError
from reranking.rerankers import RerankError


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
    config = PipelineConfig().with_overrides({"generation": {"backend": "extractive"}})

    class index:
        key = "fake-index"
        chunks = [1, 2, 3]
        meta = {}

    def answer(self, q):
        if q == "boom":
            raise RuntimeError("secret internal detail")
        if q == "llm":
            raise OllamaError("Ollama is not reachable")
        if q == "rerank":
            raise RerankError("reranker maxsim cannot be loaded: model file missing")
        if q.strip() == "":
            raise QueryError("empty question")
        return FakeAnswer()

    def retrieve(self, q, top_k=None):
        raise RerankError("reranker failed")


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(api_module.state, "load", lambda: None)  # do not load the real index
    monkeypatch.setattr(api_module.state, "history", AnswerHistory(tmp_path / "history"))
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


def test_reranker_failure_is_503_not_a_silent_fallback(client):
    api_module.state.pipeline = FakePipeline()
    r = client.post("/ask", json={"question": "rerank"})
    assert r.status_code == 503 and r.json()["error"].startswith("reranker unavailable")
    r = client.post("/search", json={"query": "anything"})
    assert r.status_code == 503 and "reranker" in r.json()["error"]


def test_answers_are_saved_and_can_be_rated(client):
    api_module.state.pipeline = FakePipeline()
    answer_id = client.post("/ask", json={"question": "How?"}).json()["answer_id"]
    saved = client.get("/history").json()["answers"][0]
    assert saved["answer_id"] == answer_id and saved["answer"] == "Tick the box [1]." and saved["rating"] is None
    assert saved["prompt"] == "strict" and saved["sources"][0]["doc_id"] == "d"

    r = client.post(f"/history/{answer_id}/rating", json={"score": 3, "flags": ["incomplete"], "comment": "no menu path"})
    assert r.status_code == 200 and r.json()["flags"] == ["incomplete"]
    assert client.get("/history").json()["answers"][0]["rating"]["score"] == 3
    group = client.get("/history/summary").json()["groups"][0]
    assert group["rated"] == 1 and group["mean_score"] == 3 and group["flags"]["incomplete"] == 1.0

    assert client.post(f"/history/{answer_id}/rating", json={"score": 3, "flags": ["typo"]}).status_code == 422
    assert client.post(f"/history/{answer_id}/rating", json={"score": 9}).status_code == 422
    assert client.post("/history/nope/rating", json={"score": 3}).status_code == 404
