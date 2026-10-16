"""Persistent embedding cache (SQLite): key = model + sha256(text).

Re-indexing with another chunking/top-k/reranker never recomputes vectors of identical texts,
and an interrupted run continues from where it stopped (every batch is committed).
On a CPU a big model embeds ~1-10 chunks/s, so this matters for the experiments.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import numpy as np

from common.utils import sha256_text
from embeddings.base import Embedder


class EmbeddingCache:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS emb (model TEXT, text_hash TEXT, dim INTEGER, vec BLOB, PRIMARY KEY (model, text_hash))"
        )
        self.conn.commit()

    def get_many(self, model: str, hashes: list[str]) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        with self._lock:
            for i in range(0, len(hashes), 500):
                part = hashes[i:i + 500]
                q = f"SELECT text_hash, dim, vec FROM emb WHERE model=? AND text_hash IN ({','.join('?' * len(part))})"
                for h, dim, blob in self.conn.execute(q, [model, *part]):
                    out[h] = np.frombuffer(blob, dtype=np.float32, count=dim)
        return out

    def put_many(self, model: str, items: list[tuple[str, np.ndarray]]) -> None:
        with self._lock:
            self.conn.executemany(
                "INSERT OR REPLACE INTO emb VALUES (?, ?, ?, ?)",
                [(model, h, int(v.shape[0]), np.asarray(v, dtype=np.float32).tobytes()) for h, v in items],
            )
            self.conn.commit()


class CachedEmbedder(Embedder):
    """Wraps any embedder; document vectors are cached, query vectors are cached too."""

    def __init__(self, inner: Embedder, cache: EmbeddingCache, model_key: str, batch_size: int = 64, progress=None) -> None:
        self.inner = inner
        self.cache = cache
        self.model_key = model_key
        self.batch_size = batch_size
        self.name = inner.name
        self.dim = inner.dim
        self.max_tokens = inner.max_tokens
        self.needs_fit = inner.needs_fit
        self.progress = progress

    def fit(self, texts: list[str]) -> None:
        self.inner.fit(texts)
        self.dim = self.inner.dim

    def state(self) -> dict:
        return self.inner.state()

    def _embed(self, texts: list[str], kind: str) -> np.ndarray:
        key = f"{self.model_key}|{kind}"
        hashes = [sha256_text(t) for t in texts]
        found = self.cache.get_many(key, list(set(hashes)))
        missing = [i for i, h in enumerate(hashes) if h not in found]
        if missing:
            uniq: dict[str, str] = {}
            for i in missing:
                uniq.setdefault(hashes[i], texts[i])
            items = list(uniq.items())
            done = 0
            for start in range(0, len(items), self.batch_size):
                batch = items[start:start + self.batch_size]
                fn = self.inner.embed_documents if kind == "doc" else (lambda xs: np.stack([self.inner.embed_query(x) for x in xs]))
                vecs = fn([t for _, t in batch])
                self.cache.put_many(key, [(h, v) for (h, _), v in zip(batch, vecs)])
                for (h, _), v in zip(batch, vecs):
                    found[h] = v
                done += len(batch)
                if self.progress:
                    self.progress(done, len(items))
        return np.stack([found[h] for h in hashes]).astype(np.float32) if hashes else np.zeros((0, self.dim), np.float32)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, "doc")

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "query")[0]
