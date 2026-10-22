"""The online RAG pipeline: question -> retrieval -> filters -> reranker -> LLM -> answer + sources.

    question
      -> query processing (validation, language detection; LLM rewrite: translation to English,
                          split into 1-3 sub-queries, Odoo terms - or translation only)
      -> Retriever        Top-K (dense / BM25 / hybrid), per sub-query
      -> Filters          metadata, length, score threshold, dedup, per-document limit
      -> Reranker         re-score, keep Top-N, optional score threshold
      -> (sub-queries?)   per-sub-query Top-N lists fused with RRF -> Top-N
      -> (nothing left?)  refusal without calling the LLM
      -> Generator        LLM answer with [n] citations, or refusal sentence
      -> Answer           text + sources (title, section, URL) + timings of every stage
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from common.config import PipelineConfig, Settings
from common.utils import detect_language, timer
from filtering.filters import apply_filters
from generation.generator import create_generator
from generation.prompts import REFUSAL, REWRITE_PROMPT, TRANSLATE_PROMPT
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
    sub_queries: list[str] = field(default_factory=list)


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
                "sub_queries": r.sub_queries,
                "retrieved": [c.to_dict(with_text=False) for c in r.retrieved],
                "dropped": r.dropped,
                "reranked": [c.to_dict(with_text=True) for c in r.reranked],
            }
        return d


_PRODUCT_RE = re.compile(r"(?:\b(?:in|for|on)\s+)?\bodoo(?:\s+\d+(?:\.\d+)?)?\b", re.I)


def strip_added_product(text: str, question: str) -> str:
    """Remove "in Odoo 18"-style words the LLM appended but the user never wrote: every
    chunk is from the Odoo docs, so they only pull generic configuration pages up."""
    text = " ".join(text.split())
    if "odoo" in question.lower():
        return text
    text = " ".join(_PRODUCT_RE.sub(" ", text).split())
    return re.sub(r"\s+([?.!,])", r"\1", text).strip(" ,")


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

    def _chat(self, prompt: str, max_tokens: int, json_format: bool = False) -> str:
        from generation.ollama_client import OllamaClient

        self._client = self._client or OllamaClient(self.settings)
        reply = self._client.chat(
            self.config.generation.model or self.settings.llm_model,
            [{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.0,
            json_format=json_format,
        )
        return reply["text"].strip()

    def _translate(self, query: str) -> str:
        from generation.ollama_client import OllamaError

        try:
            text = self._chat(TRANSLATE_PROMPT.format(question=query), max_tokens=120)
            text = text.strip('"').splitlines()[0].strip() if text else ""
            return text or query
        except OllamaError as exc:
            log.warning("query translation failed, searching with the original query: %s", exc)
            return query

    def _rewrite(self, query: str, language: str) -> list[str]:
        """LLM -> English search queries: the whole question, or one query per topic if it has several.
        Any failure falls back to translation."""
        from generation.ollama_client import OllamaError

        n = max(1, self.config.retrieval.max_subqueries)
        try:
            text = self._chat(REWRITE_PROMPT.format(question=query, max_queries=n), max_tokens=200, json_format=True)
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])

            def clean(items) -> list[str]:
                items = [strip_added_product(q, query) for q in items if isinstance(q, str)]
                return list(dict.fromkeys(q for q in items if q))  # no duplicates

            parts = clean(data.get("queries") or [])[:n]
            whole = clean([data.get("translation") or ""])
            # one topic -> search the whole question only: paraphrases of one topic, fused together,
            # pushed generic chunks above the right one (live run 2026-10-22, "storage locations")
            queries = parts if len(parts) > 1 else (whole or parts)
            if queries:
                return queries
            log.warning("query rewrite returned no queries: %r", text[:200])
        except OllamaError as exc:
            log.warning("query rewrite failed: %s", exc)
        except (ValueError, AttributeError) as exc:
            log.warning("query rewrite returned invalid JSON (%s): %r", exc, text[:200])
        return [self._translate(query) if language != "en" else query]

    def _search(self, query: str, top_k: int | None, timings: dict) -> tuple:
        """Retriever -> filters -> reranker for one search query; stage timings are added up."""
        cfg = self.config

        def add(name: str, ms: float) -> None:
            timings[name] = timings.get(name, 0.0) + ms

        with timer() as t:
            query_vec = self.retriever.embed_query(query) if cfg.retrieval.mode != "bm25" else None
        add("embed_query", t["ms"])
        with timer() as t:
            retrieved = self.retriever.retrieve(query, top_k=top_k, query_vec=query_vec)
        add("retrieve", t["ms"])
        with timer() as t:
            filtered, dropped = apply_filters(retrieved, cfg.filters)
        add("filter", t["ms"])

        # copies, so reranking does not change the retriever's list (needed for debugging/eval)
        pool = [Candidate(c.idx, c.chunk, c.score, c.rank, c.source, dict(c.scores)) for c in filtered]
        if self.reranker is not None and pool:
            with timer() as t:
                pool = self.reranker.rerank(query, pool)
            add("rerank", t["ms"])
            if cfg.reranker.min_score is not None:
                kept = [c for c in pool if c.scores.get("rerank", 0.0) >= cfg.reranker.min_score]
                for c in pool:
                    if c not in kept:
                        dropped.append({"chunk_id": c.chunk["chunk_id"], "doc_id": c.chunk["doc_id"], "rank": c.rank,
                                        "reason": f"rerank score {c.scores.get('rerank', 0):.3f} < {cfg.reranker.min_score}"})
                pool = kept
        return retrieved, filtered, dropped, pool[: cfg.reranker.top_n], query_vec

    def _fuse(self, lists: list[list[Candidate]], limit: int) -> list[Candidate]:
        """RRF over per-sub-query lists: every sub-query gets its best chunks into the result."""
        k = self.config.retrieval.rrf_k
        best: dict[str, Candidate] = {}
        fused: dict[str, float] = {}
        for cands in lists:
            for rank, c in enumerate(cands):
                cid = c.chunk["chunk_id"]
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (k + rank + 1)
                best.setdefault(cid, c)
        order = sorted(fused, key=lambda cid: -fused[cid])[:limit]
        out = []
        for cid in order:
            c = best[cid]
            out.append(Candidate(c.idx, c.chunk, c.score, c.rank, c.source, {**c.scores, "subquery_rrf": fused[cid]}))
        return out

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
        queries = [query]
        if cfg.retrieval.rewrite_query:
            with timer() as t:
                queries = self._rewrite(query, language)
            timings["rewrite"] = t["ms"]
        elif language != "en" and cfg.retrieval.translate_query:
            with timer() as t:
                queries = [self._translate(query)]
            timings["translate"] = t["ms"]

        results = [self._search(q, top_k, timings) for q in queries]
        dropped = [d for res in results for d in res[2]]
        query_vec = results[0][4]
        if len(results) == 1:
            retrieved, filtered, _, reranked, _ = results[0]
        else:  # one ranked list per stage, so per-stage metrics stay meaningful
            retrieved = self._fuse([res[0] for res in results], top_k or cfg.retrieval.top_k)
            filtered = self._fuse([res[1] for res in results], top_k or cfg.retrieval.top_k)
            reranked = self._fuse([res[3] for res in results], cfg.reranker.top_n)
        search_query = " | ".join(queries)
        return RetrievalResult(query, language, search_query, retrieved, filtered, dropped, reranked, timings,
                               query_vec, queries)

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
