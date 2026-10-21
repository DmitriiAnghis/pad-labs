"""Rerankers: re-score the filtered Top-K candidates with a slower, more precise model.

Why a second stage: the retriever compares *independent* vectors of the query and of the chunk
(bi-encoder) - fast, but the two texts never "see" each other. A cross-encoder reads the pair
(query, chunk) together and outputs one relevance score: much more precise, but it has to run once
per candidate, so it is applied only to the Top-K that the retriever found.

Backends:
* ``cross_encoder`` - sentence-transformers CrossEncoder (default BAAI/bge-reranker-v2-m3,
                      multilingual; or cross-encoder/ms-marco-MiniLM-L-6-v2). Runs on your PC.
* ``llm``           - the LLM in Ollama rates each chunk 0..10 (pointwise prompt).
* ``bi_encoder``    - MiniLM cosine between query and chunk: a neural second stage on top of a
                      lexical retriever (BM25 -> MiniLM). Runs anywhere, CPU.
* ``maxsim``        - late interaction (ColBERT-like) on MiniLM token vectors: for every query
                      token take the best-matching chunk token, average. CPU.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

import numpy as np

from common.config import RerankerConfig, Settings
from retrieval.retriever import Candidate

log = logging.getLogger("reranking")


class RerankError(RuntimeError):
    pass


class Reranker(ABC):
    name = "base"

    @abstractmethod
    def score(self, query: str, texts: list[str]) -> list[float]: ...

    def rerank(self, query: str, cands: list[Candidate]) -> list[Candidate]:
        if not cands:
            return []
        try:
            scores = self.score(query, [c.chunk["embed_text"] for c in cands])
        except RerankError:
            raise
        except Exception as exc:  # noqa: BLE001 - one error type for the API (503), no silent fallback
            raise RerankError(f"reranker {self.name} failed: {type(exc).__name__}: {exc}") from exc
        for c, s in zip(cands, scores):
            c.scores["retriever_rank"] = c.rank
            c.scores["rerank"] = float(s)
        ordered = sorted(cands, key=lambda c: -c.scores["rerank"])
        for r, c in enumerate(ordered):
            c.rank = r + 1
            c.score = c.scores["rerank"]
        return ordered


class CrossEncoderReranker(Reranker):
    _models: dict[str, object] = {}

    def __init__(self, model_name: str, batch_size: int = 16) -> None:
        self.name = f"cross-encoder:{model_name}"
        self.model_name = model_name
        self.batch_size = batch_size
        if model_name not in self._models:
            try:
                from sentence_transformers import CrossEncoder
            except ImportError as exc:
                raise RerankError("sentence-transformers is not installed: pip install -r requirements-extra.txt") from exc
            try:
                self._models[model_name] = CrossEncoder(model_name, max_length=512)
            except Exception as exc:  # noqa: BLE001
                raise RerankError(f"cannot load cross-encoder '{model_name}' (needs internet access to huggingface.co on first run): {exc}") from exc
        self.model = self._models[model_name]

    def score(self, query: str, texts: list[str]) -> list[float]:
        scores = self.model.predict([(query, t) for t in texts], batch_size=self.batch_size, show_progress_bar=False)
        return [float(s) for s in np.asarray(scores).reshape(-1)]


LLM_RERANK_PROMPT = """You judge search results for a question about the Odoo Inventory / Barcode documentation.
Question: {query}

Passage:
\"\"\"{passage}\"\"\"

How useful is this passage for answering the question? Reply with ONE integer from 0 (irrelevant) to 10 (directly answers it). Reply with the number only."""


class LLMReranker(Reranker):
    def __init__(self, client, model: str) -> None:
        self.client = client
        self.model = model
        self.name = f"llm:{model}"

    def score(self, query: str, texts: list[str]) -> list[float]:
        scores = []
        for t in texts:
            prompt = LLM_RERANK_PROMPT.format(query=query, passage=t[:2000])
            reply = self.client.chat(self.model, [{"role": "user", "content": prompt}], max_tokens=8, temperature=0.0)
            m = re.search(r"\d+(\.\d+)?", reply["text"])
            scores.append(min(10.0, float(m.group(0))) if m else 0.0)
        return scores


class BiEncoderReranker(Reranker):
    def __init__(self, minilm) -> None:
        self.model = minilm
        self.name = "bi-encoder:all-MiniLM-L6-v2"

    def score(self, query: str, texts: list[str]) -> list[float]:
        q = self.model.embed_query(query)
        d = self.model.embed_documents(texts)
        return (d @ q).tolist()


class MaxSimReranker(Reranker):
    def __init__(self, minilm) -> None:
        self.model = minilm
        self.name = "maxsim:all-MiniLM-L6-v2"

    def score(self, query: str, texts: list[str]) -> list[float]:
        q_tok = self.model.token_vectors([query])[0][1:-1]  # drop [CLS]/[SEP]
        scores = []
        for i in range(0, len(texts), 16):
            for d_tok in self.model.token_vectors(texts[i:i + 16]):
                sim = q_tok @ d_tok[1:-1].T
                scores.append(float(sim.max(axis=1).mean()) if sim.size else 0.0)
        return scores


def create_reranker(cfg: RerankerConfig, settings: Settings) -> Reranker | None:
    if cfg.backend == "none":
        return None
    if cfg.backend == "cross_encoder":
        return CrossEncoderReranker(cfg.model or settings.rerank_model, cfg.batch_size)
    if cfg.backend == "llm":
        from generation.ollama_client import OllamaClient

        return LLMReranker(OllamaClient(settings), cfg.model or settings.llm_model)
    from embeddings.factory import get_minilm

    if cfg.backend == "bi_encoder":
        return BiEncoderReranker(get_minilm(settings))
    if cfg.backend == "maxsim":
        return MaxSimReranker(get_minilm(settings))
    raise ValueError(cfg.backend)  # pragma: no cover
