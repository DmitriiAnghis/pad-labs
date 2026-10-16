from __future__ import annotations

from abc import ABC, abstractmethod

import numpy as np


class EmbeddingError(RuntimeError):
    """Readable error of an embedding backend (model missing, service down, timeout)."""


class Embedder(ABC):
    """Text -> L2-normalised float32 vectors (so inner product == cosine similarity)."""

    name: str = "base"
    dim: int = 0
    max_tokens: int | None = None  # input truncation limit of the model, for statistics
    needs_fit: bool = False  # TF-IDF/LSA has to be fitted on the corpus first

    def fit(self, texts: list[str]) -> None:  # pragma: no cover - only for fitted models
        pass

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    @abstractmethod
    def embed_query(self, text: str) -> np.ndarray: ...

    def state(self) -> dict:
        """Serializable state needed to reload a fitted model (TF-IDF)."""
        return {}


def l2_normalize(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms
