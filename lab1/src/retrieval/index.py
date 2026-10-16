"""Index = chunks + their vectors in a vector store + a BM25 index over the same chunks.

An index is identified by ``PipelineConfig.index_key()`` (processing version, chunking params,
contextual header, embedding model), e.g. ``p1__section-1000-o0-m100__h1__minilm`` and stored in
``data/indexes/<key>/``:  chunks.jsonl, vectors.npy, faiss.index, meta.json.
It is rebuilt automatically when the processed corpus changes (fingerprint in meta.json).
Retrieval-only parameters (top-k, filters, reranker) do not need a new index.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

from common.config import PipelineConfig, Settings
from common.utils import read_json, read_jsonl, timer, utc_now, write_json, write_jsonl
from embeddings.base import Embedder
from embeddings.factory import create_embedder
from preprocessing.chunking import chunk_corpus, chunk_stats
from preprocessing.documents import load_documents
from retrieval.bm25 import BM25Index
from vectorstore.store import FaissStore, VectorStore, create_store

log = logging.getLogger("index")

_memory_cache: dict[str, "RagIndex"] = {}


@dataclass
class RagIndex:
    key: str
    path: Path
    chunks: list[dict]
    bm25: BM25Index
    embedder: Optional[Embedder]
    vectors: Optional[np.ndarray]
    dense: Optional[VectorStore]
    meta: dict = field(default_factory=dict)

    def dense_scores_for(self, query_vec: np.ndarray, idxs: list[int]) -> np.ndarray:
        """Exact cosine of the query with given chunks (vectors are normalised)."""
        if self.vectors is None or not idxs:
            return np.zeros(len(idxs), dtype=np.float32)
        return self.vectors[np.asarray(idxs)] @ query_vec


def _corpus_fingerprint(settings: Settings) -> str:
    state = read_json(settings.processed_dir / "state.json", default={}) or {}
    return state.get("fingerprint", "")


def build_index(
    settings: Settings,
    config: PipelineConfig,
    force: bool = False,
    persist: bool = True,
    docs: list[dict] | None = None,
) -> RagIndex:
    key = config.index_key() + (f"__{config.vectorstore.backend}-{config.vectorstore.index_type}" if config.vectorstore.backend != "faiss" or config.vectorstore.index_type != "flat" else "")
    fingerprint = _corpus_fingerprint(settings)
    mem_key = f"{key}|{fingerprint}"
    if not force and mem_key in _memory_cache:
        return _memory_cache[mem_key]
    path = settings.indexes_dir / key
    meta = read_json(path / "meta.json", default=None)
    if not force and meta and meta.get("corpus_fingerprint") == fingerprint and (path / "chunks.jsonl").exists():
        index = _load(path, key, settings, config, meta)
        if index is not None:
            _memory_cache[mem_key] = index
            return index

    docs = docs if docs is not None else load_documents(settings)
    with timer() as t_chunk:
        chunks = chunk_corpus(docs, config.chunking)
    if not chunks:
        raise RuntimeError("chunking produced no chunks")
    texts = [c["embed_text"] for c in chunks]
    bm25 = BM25Index(texts, k1=config.retrieval.bm25.k1, b=config.retrieval.bm25.b, stem=config.retrieval.bm25.stem)

    embedder = create_embedder(config.embedding, settings)
    vectors = None
    store = None
    t_embed = {"ms": 0.0}
    if embedder is not None:
        with timer() as t_embed:
            if embedder.needs_fit:
                embedder.fit(texts)
            vectors = embedder.embed_documents(texts)
        store = create_store(config.vectorstore, vectors.shape[1], path, name=key[:60])
        store.add(vectors)
        log.info("index %s: %d chunks embedded with %s in %.1fs", key, len(chunks), embedder.name, t_embed["ms"] / 1000)

    meta = {
        "key": key,
        "built_at": utc_now(),
        "corpus_fingerprint": fingerprint,
        "chunks": len(chunks),
        "chunk_stats": chunk_stats(chunks),
        "embedder": embedder.name if embedder else None,
        "dim": int(vectors.shape[1]) if vectors is not None else 0,
        "embed_seconds": round(t_embed["ms"] / 1000, 2),
        "chunk_seconds": round(t_chunk["ms"] / 1000, 2),
        "truncated_inputs": getattr(getattr(embedder, "inner", embedder), "truncated", None),
        "config": {
            "chunking": config.chunking.model_dump(),
            "embedding": config.embedding.model_dump(),
            "vectorstore": config.vectorstore.model_dump(),
        },
    }
    index = RagIndex(key, path, chunks, bm25, embedder, vectors, store, meta)
    if persist:
        path.mkdir(parents=True, exist_ok=True)
        write_jsonl(path / "chunks.jsonl", chunks)
        if vectors is not None:
            np.save(path / "vectors.npy", vectors)
            if isinstance(store, FaissStore):
                store.save(path)
        write_json(path / "meta.json", meta)
    _memory_cache[mem_key] = index
    return index


def _load(path: Path, key: str, settings: Settings, config: PipelineConfig, meta: dict) -> Optional[RagIndex]:
    chunks = read_jsonl(path / "chunks.jsonl")
    texts = [c["embed_text"] for c in chunks]
    bm25 = BM25Index(texts, k1=config.retrieval.bm25.k1, b=config.retrieval.bm25.b, stem=config.retrieval.bm25.stem)
    embedder = create_embedder(config.embedding, settings)
    vectors = None
    store = None
    if embedder is not None:
        if embedder.needs_fit:  # TF-IDF/LSA: refit (deterministic, < 1 s) instead of pickling sklearn objects
            embedder.fit(texts)
        vec_path = path / "vectors.npy"
        if not vec_path.exists():
            return None
        vectors = np.load(vec_path)
        if config.vectorstore.backend == "faiss" and (path / "faiss.index").exists():
            store = FaissStore.load(path, vectors.shape[1], config.vectorstore.index_type)
        else:
            store = create_store(config.vectorstore, vectors.shape[1], path, name=key[:60])
            store.add(vectors)
    log.info("loaded index %s (%d chunks)", key, len(chunks))
    return RagIndex(key, path, chunks, bm25, embedder, vectors, store, meta)
