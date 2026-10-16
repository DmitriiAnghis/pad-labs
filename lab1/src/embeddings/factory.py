from __future__ import annotations

import logging

from common.config import EmbeddingConfig, Settings
from embeddings.base import Embedder
from embeddings.cache import CachedEmbedder, EmbeddingCache

log = logging.getLogger("embeddings")

_minilm_singleton = None
_cache_singleton: dict[str, EmbeddingCache] = {}


def get_minilm(settings: Settings):
    """One ONNX session per process (loading takes ~0.5 s and 90 MB of RAM)."""
    global _minilm_singleton
    if _minilm_singleton is None:
        from embeddings.minilm import MiniLMEmbedder

        _minilm_singleton = MiniLMEmbedder(settings.models_dir)
    return _minilm_singleton


def get_cache(settings: Settings) -> EmbeddingCache:
    path = str(settings.cache_dir / "embeddings.sqlite")
    if path not in _cache_singleton:
        _cache_singleton[path] = EmbeddingCache(settings.cache_dir / "embeddings.sqlite")
    return _cache_singleton[path]


def _progress(done: int, total: int) -> None:
    if total >= 64 and (done == total or done % max(64, total // 10) < 32):
        log.info("embedding: %d/%d", done, total)


def create_embedder(cfg: EmbeddingConfig, settings: Settings) -> Embedder | None:
    if cfg.backend == "none":
        return None
    if cfg.backend == "tfidf":
        from embeddings.tfidf import TfidfLsaEmbedder

        return TfidfLsaEmbedder(dim=cfg.dim)  # fitted per index, not cached (it is corpus-specific)
    if cfg.backend == "minilm":
        inner = get_minilm(settings)
    elif cfg.backend == "ollama":
        from embeddings.ollama import OllamaEmbedder

        inner = OllamaEmbedder(
            settings.ollama_url,
            cfg.model or settings.embed_model,
            timeout=settings.ollama_timeout,
            query_prefix=cfg.query_prefix,
            doc_prefix=cfg.doc_prefix,
        )
    else:  # pragma: no cover
        raise ValueError(cfg.backend)
    model_key = f"{cfg.key()}|q:{cfg.query_prefix}|d:{cfg.doc_prefix}"
    return CachedEmbedder(inner, get_cache(settings), model_key, batch_size=cfg.batch_size, progress=_progress)
