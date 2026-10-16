"""Embeddings served by Ollama (/api/embed): bge-m3, nomic-embed-text, mxbai-embed-large, ...

Runs on the machine with Ollama (your PC). Model is chosen by EMBED_MODEL or embedding.model.
Some models need task prefixes (nomic: "search_query: " / "search_document: ") - see configs.
"""
from __future__ import annotations

import logging

import numpy as np
import requests

from embeddings.base import Embedder, EmbeddingError, l2_normalize

log = logging.getLogger("embeddings.ollama")


class OllamaEmbedder(Embedder):
    def __init__(self, base_url: str, model: str, timeout: float = 180, query_prefix: str = "", doc_prefix: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.query_prefix = query_prefix
        self.doc_prefix = doc_prefix
        self.name = f"ollama:{model}"
        self.dim = 0

    def _post(self, inputs: list[str]) -> np.ndarray:
        url = f"{self.base_url}/api/embed"
        try:
            resp = requests.post(url, json={"model": self.model, "input": inputs, "truncate": True}, timeout=self.timeout)
        except requests.Timeout as exc:
            raise EmbeddingError(f"Ollama embedding timeout after {self.timeout}s ({url})") from exc
        except requests.ConnectionError as exc:
            raise EmbeddingError(f"Ollama is not reachable at {self.base_url} - is it running? (OLLAMA_URL)") from exc
        if resp.status_code == 404:
            raise EmbeddingError(f"embedding model '{self.model}' not found in Ollama. Run: ollama pull {self.model}")
        if resp.status_code >= 400:
            raise EmbeddingError(f"Ollama /api/embed error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        vectors = np.asarray(data.get("embeddings") or [], dtype=np.float32)
        if vectors.ndim != 2 or len(vectors) != len(inputs):
            raise EmbeddingError(f"unexpected Ollama embedding response for {len(inputs)} inputs")
        self.dim = vectors.shape[1]
        return l2_normalize(vectors)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._post([self.doc_prefix + t for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._post([self.query_prefix + text])[0]
