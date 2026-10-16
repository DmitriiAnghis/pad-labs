"""End-to-end pipeline on a tiny corpus with TF-IDF embeddings (no network, no model download)."""
import pytest
import requests

from generation.generator import ExtractiveGenerator, GenerationResult, OllamaGenerator, build_context, parse_citations
from generation.ollama_client import OllamaClient, OllamaError
from generation.prompts import REFUSAL, is_refusal
from pipeline.rag import QueryError, RagPipeline
from retrieval.index import build_index


@pytest.fixture
def pipeline(settings, config, small_corpus):
    cfg = config.with_overrides({
        "embedding": {"backend": "tfidf", "dim": 8},
        "chunking": {"strategy": "section", "max_chars": 400, "min_chars": 10},
        "generation": {"backend": "extractive"},
        "filters": {"min_chars": 10},
    })
    index = build_index(settings, cfg, force=True, persist=False, docs=small_corpus)
    return RagPipeline(settings, cfg, index)


class ExplodingGenerator:
    model = "must-not-be-called"

    def generate(self, *a, **kw):
        raise AssertionError("the LLM must not be called without context")


def test_hybrid_search_finds_the_right_document(pipeline):
    r = pipeline.retrieve("How do I enable wave picking?")
    assert r.reranked[0].chunk["doc_id"] == "inventory/wave"
    assert {"retrieve", "filter"} <= set(r.timings_ms)


def test_answer_has_sources_and_citations(pipeline):
    ans = pipeline.answer("Which RFID encoding scheme is supported?")
    assert not ans.refused
    assert ans.sources and ans.sources[0]["doc_id"] == "barcode/rfid"
    assert any(s["cited"] for s in ans.sources)
    assert "[1]" in ans.text


def test_refusal_without_llm_when_nothing_passes_the_threshold(pipeline):
    pipeline.config = pipeline.config.with_overrides({"filters": {"min_score": 0.999}})
    pipeline._generator = ExplodingGenerator()
    ans = pipeline.answer("How do I configure payroll?")
    assert ans.refused and ans.refusal_reason == "no_relevant_context"
    assert ans.text == REFUSAL["en"] and ans.sources == []


def test_refusal_is_in_the_language_of_the_question(pipeline):
    pipeline.config = pipeline.config.with_overrides({"filters": {"min_score": 0.999}})
    ans = pipeline.answer("Как настроить зарплату?")
    assert ans.language == "ru" and ans.text == REFUSAL["ru"]


@pytest.mark.parametrize("bad", ["", "   ", "x" * 1001])
def test_invalid_questions_are_rejected(pipeline, bad):
    with pytest.raises(QueryError):
        pipeline.retrieve(bad)


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def chat(self, model, messages, **kw):
        self.calls.append(messages)
        return {"text": self.text, "model": model, "ms": 1.0}


def test_llm_refusal_is_detected_and_sources_dropped(pipeline, settings):
    pipeline._generator = OllamaGenerator(settings, pipeline.config.generation, client=FakeClient(REFUSAL["en"]))
    ans = pipeline.answer("How do I enable wave picking?")
    assert ans.refused and ans.refusal_reason == "llm_refused" and ans.sources == []


def test_llm_prompt_contains_numbered_context(pipeline, settings):
    client = FakeClient("Tick Batch, Wave & Cluster Transfers [1].")
    pipeline._generator = OllamaGenerator(settings, pipeline.config.generation, client=client)
    ans = pipeline.answer("How do I enable wave picking?")
    user = client.calls[0][1]["content"]
    assert "[1] Wave transfers" in user and "Question: How do I enable wave picking?" in user
    assert not ans.refused and ans.sources[0]["cited"]


def test_parse_citations_ignores_out_of_range():
    assert parse_citations("a [1] b [2][7] c [1, 3]", 3) == [1, 2, 3]


def test_is_refusal():
    assert is_refusal(REFUSAL["en"]) and is_refusal(REFUSAL["ru"])
    assert not is_refusal("Go to Inventory > Configuration > Settings [1].")


def test_ollama_errors_are_readable(settings, monkeypatch):
    def refuse(*a, **kw):
        raise requests.ConnectionError("boom")

    monkeypatch.setattr(requests, "post", refuse)
    with pytest.raises(OllamaError, match="not reachable"):
        OllamaClient(settings).chat("m", [{"role": "user", "content": "hi"}])

    class Resp:
        status_code = 404
        text = "model not found"

    monkeypatch.setattr(requests, "post", lambda *a, **kw: Resp())
    with pytest.raises(OllamaError, match="ollama pull m"):
        OllamaClient(settings).chat("m", [{"role": "user", "content": "hi"}])


def test_think_blocks_are_stripped(settings, monkeypatch):
    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"message": {"content": "<think>reasoning</think>\nAnswer [1]"}, "model": "m"}

    monkeypatch.setattr(requests, "post", lambda *a, **kw: Resp())
    assert OllamaClient(settings).chat("m", [])["text"] == "Answer [1]"
