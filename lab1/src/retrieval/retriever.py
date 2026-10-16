"""Retriever: query -> Top-K chunks.

Modes:
* ``dense``  - query embedding -> nearest chunk vectors (cosine) in the vector store;
* ``bm25``   - lexical BM25;
* ``hybrid`` - both lists fused with Reciprocal Rank Fusion:
               score(d) = sum_i 1 / (rrf_k + rank_i(d)).
  RRF uses only ranks, so cosine (0..1) and BM25 (0..30) scores need no calibration;
  a chunk found by both retrievers rises to the top.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

from common.config import RetrievalConfig
from retrieval.index import RagIndex


@dataclass
class Candidate:
    idx: int
    chunk: dict
    score: float
    rank: int = 0
    source: str = ""
    scores: dict = field(default_factory=dict)  # dense / bm25 / rrf / rerank

    def to_dict(self, with_text: bool = True) -> dict:
        d = {
            "rank": self.rank,
            "score": round(float(self.score), 5),
            "scores": {k: round(float(v), 5) for k, v in self.scores.items() if v is not None},
            "source": self.source,
            "chunk_id": self.chunk["chunk_id"],
            "doc_id": self.chunk["doc_id"],
            "title": self.chunk["title"],
            "section": self.chunk["section"],
            "url": self.chunk["url"],
        }
        if with_text:
            d["text"] = self.chunk["text"]
        return d


class Retriever:
    def __init__(self, index: RagIndex, cfg: RetrievalConfig) -> None:
        self.index = index
        self.cfg = cfg
        if cfg.mode in ("dense", "hybrid") and index.dense is None:
            raise ValueError(f"retrieval mode '{cfg.mode}' needs an embedding model (embedding.backend is 'none')")

    def embed_query(self, query: str) -> Optional[np.ndarray]:
        if self.index.embedder is None:
            return None
        return self.index.embedder.embed_query(query)

    def retrieve(self, query: str, top_k: int | None = None, query_vec: Optional[np.ndarray] = None) -> list[Candidate]:
        k = top_k or self.cfg.top_k
        chunks = self.index.chunks
        mode = self.cfg.mode
        if mode in ("dense", "hybrid") and query_vec is None:
            query_vec = self.embed_query(query)

        if mode == "dense":
            hits = self.index.dense.search(query_vec, k)
            cands = [Candidate(i, chunks[i], s, r + 1, "dense", {"dense": s}) for r, (i, s) in enumerate(hits)]
        elif mode == "bm25":
            hits = self.index.bm25.search(query, k)
            cands = [Candidate(i, chunks[i], s, r + 1, "bm25", {"bm25": s}) for r, (i, s) in enumerate(hits)]
        else:
            depth = max(k, self.cfg.candidates_per_retriever)
            dense_hits = self.index.dense.search(query_vec, depth)
            bm25_hits = self.index.bm25.search(query, depth)
            fused: dict[int, float] = {}
            info: dict[int, dict] = {}
            for name, hits in (("dense", dense_hits), ("bm25", bm25_hits)):
                for r, (i, s) in enumerate(hits):
                    fused[i] = fused.get(i, 0.0) + 1.0 / (self.cfg.rrf_k + r + 1)
                    info.setdefault(i, {})[name] = s
                    info[i][f"{name}_rank"] = r + 1
            order = sorted(fused, key=lambda i: -fused[i])[:k]
            cos = self.index.dense_scores_for(query_vec, order)
            cands = []
            for r, (i, c) in enumerate(zip(order, cos)):
                scores = {"rrf": fused[i], "dense": float(c)}
                if "bm25" in info[i]:
                    scores["bm25"] = info[i]["bm25"]
                cands.append(Candidate(i, chunks[i], fused[i], r + 1, "hybrid", scores))
        return cands
