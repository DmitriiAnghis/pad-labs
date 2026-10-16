"""Vector stores behind one small interface.

* ``FaissStore`` (default) - in-process FAISS. ``flat`` = exact inner-product search: with ~1k
  chunks an exact scan takes well under 1 ms, so an approximate index buys nothing and costs
  recall. ``hnsw`` is kept for the comparison experiment (and for a much larger corpus).
* ``QdrantStore`` - Qdrant client in local file mode (no server) or against a Qdrant server
  (QDRANT_URL). This is the natural choice once the RAG becomes a separate service (labs 2-3):
  the vector DB is its own container with its own API.
"""
from __future__ import annotations

import atexit
import logging
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np

log = logging.getLogger("vectorstore")


class VectorStore(ABC):
    @abstractmethod
    def add(self, vectors: np.ndarray) -> None: ...

    @abstractmethod
    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]: ...

    def save(self, path: Path) -> None:  # pragma: no cover - optional
        pass

    @property
    @abstractmethod
    def size(self) -> int: ...


class FaissStore(VectorStore):
    def __init__(self, dim: int, index_type: str = "flat", hnsw_m: int = 32) -> None:
        import faiss

        self.dim = dim
        self.index_type = index_type
        if index_type == "hnsw":
            self.index = faiss.IndexHNSWFlat(dim, hnsw_m, faiss.METRIC_INNER_PRODUCT)
            self.index.hnsw.efSearch = 64
            self.index.hnsw.efConstruction = 80
        else:
            self.index = faiss.IndexFlatIP(dim)

    def add(self, vectors: np.ndarray) -> None:
        self.index.add(np.ascontiguousarray(vectors, dtype=np.float32))

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        k = min(k, self.index.ntotal)
        if k <= 0:
            return []
        scores, ids = self.index.search(np.ascontiguousarray(query.reshape(1, -1), dtype=np.float32), k)
        return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i != -1]

    @property
    def size(self) -> int:
        return int(self.index.ntotal)

    def save(self, path: Path) -> None:
        import faiss

        faiss.write_index(self.index, str(path / "faiss.index"))

    @classmethod
    def load(cls, path: Path, dim: int, index_type: str) -> "FaissStore":
        import faiss

        store = cls.__new__(cls)
        store.dim = dim
        store.index_type = index_type
        store.index = faiss.read_index(str(path / "faiss.index"))
        return store


class QdrantStore(VectorStore):
    def __init__(self, dim: int, collection: str, path: Path | None = None, url: str | None = None) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("qdrant-client is not installed: pip install -r requirements-extra.txt") from exc
        self.client = QdrantClient(url=url) if url else QdrantClient(path=str(path))
        atexit.register(self.client.close)  # close the local storage before interpreter shutdown
        self.collection = collection
        self.dim = dim
        if self.client.collection_exists(collection):
            self.client.delete_collection(collection)
        self.client.create_collection(collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
        self._n = 0

    def add(self, vectors: np.ndarray) -> None:
        from qdrant_client.models import PointStruct

        points = [PointStruct(id=self._n + i, vector=v.tolist(), payload={}) for i, v in enumerate(vectors)]
        for i in range(0, len(points), 256):
            self.client.upsert(self.collection, points[i:i + 256])
        self._n += len(points)

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        res = self.client.query_points(self.collection, query=query.tolist(), limit=k).points
        return [(int(p.id), float(p.score)) for p in res]

    @property
    def size(self) -> int:
        return self._n


def create_store(cfg, dim: int, path: Path | None = None, name: str = "chunks") -> VectorStore:
    if cfg.backend == "qdrant":
        return QdrantStore(dim, collection=name, path=(path / "qdrant") if path else None, url=cfg.qdrant_url)
    return FaissStore(dim, cfg.index_type, cfg.hnsw_m)
