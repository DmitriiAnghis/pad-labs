"""Tracing: steps of one answer, Langfuse ingestion events, and Langfuse failures never breaking an answer."""
import threading

import pytest
import requests

from common import tracing
from generation.generator import OllamaGenerator
from pipeline.rag import RagPipeline
from retrieval.index import build_index


class FakeClient:
    """Ollama stand-in that records generations the way OllamaClient does."""

    def __init__(self, text):
        self.text = text

    def chat(self, model, messages, name="llm", **kw):
        start = tracing.time.time()
        tracing.record_generation(name, model, messages, self.text, start, usage={"input": 10, "output": 5, "unit": "TOKENS"})
        return {"text": self.text, "model": model, "ms": 1.0}


@pytest.fixture
def pipeline(settings, config, small_corpus):
    cfg = config.with_overrides({
        "embedding": {"backend": "tfidf", "dim": 8},
        "chunking": {"strategy": "section", "max_chars": 400, "min_chars": 10},
        "generation": {"backend": "ollama"},
        "filters": {"min_chars": 10},
    })
    index = build_index(settings, cfg, force=True, persist=False, docs=small_corpus)
    p = RagPipeline(settings, cfg, index)
    p._generator = OllamaGenerator(settings, cfg.generation, client=FakeClient("Tick the checkbox [1]."))
    return p


def test_answer_carries_a_trace_with_every_stage(pipeline):
    ans = pipeline.answer("How do I enable wave picking?")
    names = [s["name"] for s in ans.trace.steps()]
    assert names[:2] == ["query", "search"]
    assert {"embed_query", "retrieve", "filter", "answer"} <= set(names)  # reranker is off in the test config
    answer_step = next(s for s in ans.trace.steps() if s["name"] == "answer")
    assert answer_step["type"] == "generation" and answer_step["tokens"]["input"] == 10
    by_name = {o.name: o for o in ans.trace.observations}
    assert by_name["retrieve"].parent_id == by_name["search"].id  # nesting is kept
    assert ans.to_dict()["trace"]["url"] is None  # Langfuse is off in tests
    assert ans.trace.output == ans.text and "answered" in ans.trace.tags


def test_refusal_without_context_is_traced_without_an_llm_call(pipeline):
    pipeline.config = pipeline.config.with_overrides({"filters": {"min_score": 0.999}})
    ans = pipeline.answer("How do I configure payroll?")
    names = [s["name"] for s in ans.trace.steps()]
    assert "refuse" in names and "answer" not in names and "refused" in ans.trace.tags


def test_spans_outside_a_trace_are_harmless(pipeline):
    r = pipeline.retrieve("How do I enable wave picking?")  # evaluation path: no trace
    assert r.reranked and tracing.current() is None


def test_langfuse_events_match_the_ingestion_api():
    with tracing.trace("rag-answer", input="q") as tr:
        with tracing.span("search", input="q") as sp:
            sp.output = [{"chunk_id": "c"}]
        tracing.record_generation("answer", "qwen", [{"role": "user", "content": "q"}], "a", tr.start,
                                  usage={"input": 3, "output": 1, "unit": "TOKENS"})
        tr.output = "a"
    events = tracing.LangfuseExporter.events(tr)
    assert [e["type"] for e in events] == ["trace-create", "span-create", "generation-create"]
    assert all(e["body"].get("traceId", tr.id) == tr.id for e in events)
    gen = events[2]["body"]
    assert gen["model"] == "qwen" and gen["usage"]["input"] == 3 and gen["startTime"].endswith("Z")


def test_unreachable_langfuse_never_breaks_the_answer(pipeline, monkeypatch):
    posted = threading.Event()

    def down(*a, **kw):
        posted.set()
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "get", down)
    monkeypatch.setattr(requests, "post", down)
    pipeline.langfuse = tracing.LangfuseExporter("http://127.0.0.1:9", "pk-lf-test", "sk-lf-test")
    ans = pipeline.answer("How do I enable wave picking?")
    assert not ans.refused
    assert ans.trace.url == f"http://127.0.0.1:9/trace/{ans.trace.id}"  # project unknown -> generic link
    assert posted.wait(5)


def test_langfuse_is_off_without_keys(settings):
    assert tracing.LangfuseExporter.from_settings(settings) is None
    on = settings.model_copy(update={"langfuse_public_key": "pk-lf-x", "langfuse_secret_key": "sk-lf-x"})
    assert tracing.LangfuseExporter.from_settings(on).host == settings.langfuse_host
