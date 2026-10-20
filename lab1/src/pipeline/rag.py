"""The online RAG pipeline: question -> retrieval -> filters -> reranker -> LLM -> answer + sources.

    question
      -> query processing (validation, language detection, optional translation to English)
      -> Retriever        Top-K (dense / BM25 / hybrid)
      -> Filters          metadata, length, score threshold, dedup, per-document limit
      -> Reranker         re-score, keep Top-N, optional score threshold
      -> (nothing left?)  refusal without calling the LLM
      -> Generator        LLM answer with [n] citations, or refusal sentence
      -> Answer           text + sources (title, section, URL) + timings of every stage
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from common.config import PipelineConfig, Settings
from common.utils import detect_language, timer
from filtering.filters import apply_filters
from generation.generator import create_generator
from generation.prompts import REFUSAL, TRANSLATE_PROMPT
from reranking.rerankers import create_reranker
from retrieval.index import RagIndex, build_index
from retrieval.retriever import Candidate, Retriever

log = logging.getLogger("pipeline")

MAX_QUERY_CHARS = 1000


class QueryError(ValueError):
    pass


@dataclass
class RetrievalResult:
    query: str
    language: str
    search_query: str
    retrieved: list[Candidate] = field(default_factory=list)
    filtered: list[Candidate] = field(default_factory=list)
    dropped: list[dict] = field(default_factory=list)
    reranked: list[Candidate] = field(default_factory=list)
    timings_ms: dict = field(default_factory=dict)
    query_vec: Optional[np.ndarray] = None


@dataclass
class Answer:
    query: str
    text: str
    refused: bool
    refusal_reason: Optional[str]
    sources: list[dict]
    model: str
    language: str
    timings_ms: dict
    retrieval: RetrievalResult

    def to_dict(self, debug: bool = False) -> dict:
        d = {
            "query": self.query,
            "answer": self.text,
            "refused": self.refused,
            "refusal_reason": self.refusal_reason,
            "language": self.language,
            "sources": self.sources,
            "model": self.model,
            "timings_ms": {k: round(v, 1) for k, v in self.timings_ms.items()},
        }
        if debug:
            r = self.retrieval
            d["debug"] = {
                "search_query": r.search_query,
                "retrieved": [c.to_dict(with_text=False) for c in r.retrieved],
                "dropped": r.dropped,
                "reranked": [c.to_dict(with_text=True) for c in r.reranked],
            }
        return d


class RagPipeline:
    def __init__(self, settings: Settings, config: PipelineConfig, index: RagIndex) -> None:
        self.settings = settings
        self.config = config
        self.index = index
        self.retriever = Retriever(index, config.retrieval)
        self._reranker = None
        self._reranker_ready = False
        self._generator = None
        self._client = None

    @classmethod
    def from_config(cls, settings: Settings, config: PipelineConfig) -> "RagPipeline":
        return cls(settings, config, build_index(settings, config))

    # lazy: the API can start (and answer /health) before heavy models are loaded
    @property
    def reranker(self):
        if not self._reranker_ready:
            self._reranker = create_reranker(self.config.reranker, self.settings)
            self._reranker_ready = True
        return self._reranker

    @property
    def generator(self):
        if self._generator is None:
            self._generator = create_generator(self.settings, self.config.generation)
        return self._generator

    def _translate(self, query: str) -> str:
        from generation.ollama_client import OllamaClient, OllamaError

        self._client = self._client or OllamaClient(self.settings)
        try:
            reply = self._client.chat(
                self.config.generation.model or self.settings.llm_model,
                [{"role": "user", "content": TRANSLATE_PROMPT.format(question=query)}],
                max_tokens=120,
                temperature=0.0,
            )
            text = reply["text"].strip().strip('"').splitlines()[0].strip() if reply["text"].strip() else ""
            return text or query
        except OllamaError as exc:
            log.warning("query translation failed, searching with the original query: %s", exc)
            return query

    # ------------------------------------------------------------------ retrieval
    def retrieve(self, query: str, top_k: int | None = None) -> RetrievalResult:
        query = (query or "").strip()
        if not query:
            raise QueryError("empty question")
        if len(query) > MAX_QUERY_CHARS:
            raise QueryError(f"question is too long ({len(query)} > {MAX_QUERY_CHARS} characters)")
        cfg = self.config
        timings: dict[str, float] = {}
        language = detect_language(query)
        search_query = query
        if language != "en" and cfg.retrieval.translate_query:
            with timer() as t:
                search_query = self._translate(query)
            timings["translate"] = t["ms"]

        with timer() as t:
            query_vec = self.retriever.embed_query(search_query) if cfg.retrieval.mode != "bm25" else None
        timings["embed_query"] = t["ms"]
        with timer() as t:
            retrieved = self.retriever.retrieve(search_query, top_k=top_k, query_vec=query_vec)
        timings["retrieve"] = t["ms"]
        with timer() as t:
            filtered, dropped = apply_filters(retrieved, cfg.filters)
        timings["filter"] = t["ms"]

        # copies, so reranking does not change the retriever's list (needed for debugging/eval)
        pool = [Candidate(c.idx, c.chunk, c.score, c.rank, c.source, dict(c.scores)) for c in filtered]
        rerank_langs = cfg.reranker.languages
        if self.reranker is not None and pool and (not rerank_langs or language in rerank_langs):
            with timer() as t:
                pool = self.reranker.rerank(search_query, pool)
            timings["rerank"] = t["ms"]
            if cfg.reranker.min_score is not None:
                kept = [c for c in pool if c.scores.get("rerank", 0.0) >= cfg.reranker.min_score]
                for c in pool:
                    if c not in kept:
                        dropped.append({"chunk_id": c.chunk["chunk_id"], "doc_id": c.chunk["doc_id"], "rank": c.rank,
                                        "reason": f"rerank score {c.scores.get('rerank', 0):.3f} < {cfg.reranker.min_score}"})
                pool = kept
        reranked = pool[: cfg.reranker.top_n]
        return RetrievalResult(query, language, search_query, retrieved, filtered, dropped, reranked, timings, query_vec)

    # ------------------------------------------------------------------ answer
    def answer(self, query: str) -> Answer:
        with timer() as total:
            r = self.retrieve(query)
            language = self.config.generation.answer_language
            language = r.language if language == "auto" else language
            timings = dict(r.timings_ms)
            if not r.reranked:
                text = REFUSAL.get(language, REFUSAL["en"])
                ans = Answer(query, text, True, "no_relevant_context", [], "none", language, timings, r)
            else:
                with timer() as t:
                    gen = self.generator.generate(query, r.reranked, language)
                timings["generate"] = t["ms"]
                from generation.generator import build_context

                _, used = build_context(r.reranked, self.config.generation.max_context_chars)
                sources = []
                for n, c in enumerate(used, start=1):
                    sources.append({
                        "n": n,
                        "cited": n in gen.cited,
                        "title": c.chunk["title"],
                        "section": c.chunk["section"],
                        "url": c.chunk["url"],
                        "doc_id": c.chunk["doc_id"],
                        "chunk_id": c.chunk["chunk_id"],
                        "score": round(float(c.score), 4),
                    })
                if gen.refused:
                    sources = []
                ans = Answer(query, gen.text, gen.refused, "llm_refused" if gen.refused else None, sources, gen.model, language, timings, r)
        ans.timings_ms["total"] = total["ms"]
        return ans
