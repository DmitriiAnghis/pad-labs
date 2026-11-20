"""Configuration.

Two layers, on purpose:
* ``Settings`` - deployment settings from environment variables / ``.env``
  (URLs, model names, timeouts, paths). In labs 2-3 these become ConfigMap/Secret values.
* ``PipelineConfig`` - research parameters of the RAG pipeline from YAML
  (chunking, retrieval, filters, reranker, generation). Experiments override them.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field

LAB_ROOT = Path(__file__).resolve().parents[2]

try:  # python-dotenv is optional at import time (tests may run without it)
    from dotenv import load_dotenv

    load_dotenv(LAB_ROOT / ".env", override=False)
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _env_bool(name: str) -> Optional[bool]:
    value = os.environ.get(name, "").strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    return None


class Settings(BaseModel):
    data_dir: Path
    config_path: Path
    ollama_url: str
    llm_model: str
    llm_model_alt: str
    judge_model: str
    embed_model: str
    ollama_timeout: float
    llm_temperature: float
    llm_num_ctx: int
    llm_think: Optional[bool]
    rerank_model: str
    source_repo_url: str
    source_branch: str
    api_host: str
    api_port: int
    log_level: str
    log_format: str
    langfuse_host: str
    langfuse_public_key: Optional[str]
    langfuse_secret_key: Optional[str]

    @classmethod
    def from_env(cls) -> "Settings":
        data_dir = Path(_env("DATA_DIR", str(LAB_ROOT / "data")))
        if not data_dir.is_absolute():
            data_dir = LAB_ROOT / data_dir
        config_path = Path(_env("RAG_CONFIG", str(LAB_ROOT / "configs" / "default.yaml")))
        if not config_path.is_absolute():
            config_path = LAB_ROOT / config_path
        return cls(
            data_dir=data_dir,
            config_path=config_path,
            ollama_url=_env("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            llm_model=_env("LLM_MODEL", "qwen3.5:4b"),
            llm_model_alt=_env("LLM_MODEL_ALT", "qwen3.5:9b"),
            judge_model=_env("JUDGE_MODEL", _env("LLM_MODEL_ALT", "qwen3.5:9b")),
            embed_model=_env("EMBED_MODEL", "bge-m3"),
            ollama_timeout=float(_env("OLLAMA_TIMEOUT", "180")),
            llm_temperature=float(_env("LLM_TEMPERATURE", "0")),
            llm_num_ctx=int(_env("LLM_NUM_CTX", "8192")),
            llm_think=_env_bool("LLM_THINK"),
            rerank_model=_env("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
            source_repo_url=_env("SOURCE_REPO_URL", "https://github.com/odoo/documentation.git"),
            source_branch=_env("SOURCE_BRANCH", "18.0"),
            api_host=_env("API_HOST", "127.0.0.1"),
            api_port=int(_env("API_PORT", "8000")),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            log_format=_env("LOG_FORMAT", "text").lower(),
            langfuse_host=_env("LANGFUSE_HOST", "http://localhost:3000").rstrip("/"),
            langfuse_public_key=os.environ.get("LANGFUSE_PUBLIC_KEY") or None,
            langfuse_secret_key=os.environ.get("LANGFUSE_SECRET_KEY") or None,
        )

    # Well-known sub-directories of DATA_DIR
    @property
    def sources_dir(self) -> Path:
        return self.data_dir / "sources"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def processed_dir(self) -> Path:
        return self.data_dir / "processed"

    @property
    def chunks_dir(self) -> Path:
        return self.data_dir / "chunks"

    @property
    def indexes_dir(self) -> Path:
        return self.data_dir / "indexes"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def eval_dir(self) -> Path:
        return self.data_dir / "eval"

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"


# ---------------------------------------------------------------- pipeline config


class SourceConfig(BaseModel):
    name: str = "odoo-docs"
    # paths inside the repository that the grabber downloads (git sparse checkout)
    include: list[str] = Field(
        default_factory=lambda: [
            "content/applications/inventory_and_mrp/inventory",
            "content/applications/inventory_and_mrp/barcode",
        ]
    )
    extensions: list[str] = Field(default_factory=lambda: [".rst"])
    # path prefix that is stripped to build the short document id
    id_prefix: str = "content/applications/inventory_and_mrp/"
    # public site: content/<x>.rst -> <doc_base_url>/<x>.html
    doc_base_url: str = "https://www.odoo.com/documentation/{branch}"
    git_timeout: int = 300
    retries: int = 3


class ProcessingConfig(BaseModel):
    version: int = 1
    keep_image_alt: bool = False  # alt texts of screenshots are mostly UI descriptions
    keep_seealso: bool = True
    min_doc_chars: int = 200  # index pages that only contain a toctree are dropped


class ChunkingConfig(BaseModel):
    strategy: Literal["fixed", "fixed_overlap", "paragraph", "section"] = "section"
    max_chars: int = 1000
    overlap: int = 0  # characters, only for fixed_overlap
    min_chars: int = 100  # smaller pieces are merged into a neighbour
    add_header: bool = True  # prepend "Doc title > Section" to the text that is embedded

    def key(self) -> str:
        overlap = self.overlap if self.strategy == "fixed_overlap" else 0
        return f"{self.strategy}-{self.max_chars}-o{overlap}-m{self.min_chars}"


class EmbeddingConfig(BaseModel):
    # tfidf: TF-IDF + LSA (classic baseline, fitted on the corpus)
    # minilm: all-MiniLM-L6-v2 ONNX (local, CPU)
    # ollama: any embedding model served by Ollama (EMBED_MODEL / model)
    # none: lexical-only pipeline (BM25)
    backend: Literal["tfidf", "minilm", "ollama", "none"] = "minilm"
    model: Optional[str] = None  # ollama model tag; default EMBED_MODEL
    dim: int = 256  # only for tfidf (LSA components)
    batch_size: int = 32
    query_prefix: str = ""  # e.g. "search_query: " for nomic-embed-text
    doc_prefix: str = ""  # e.g. "search_document: "

    def key(self) -> str:
        if self.backend == "tfidf":
            return f"tfidf-lsa{self.dim}"
        if self.backend == "ollama":
            return "ollama-" + (self.model or os.environ.get("EMBED_MODEL") or "bge-m3").replace(":", "_").replace("/", "_")
        return self.backend


class VectorStoreConfig(BaseModel):
    backend: Literal["faiss", "qdrant"] = "faiss"
    index_type: Literal["flat", "hnsw"] = "flat"
    hnsw_m: int = 32
    qdrant_url: Optional[str] = None  # None -> local file mode


class BM25Config(BaseModel):
    k1: float = 1.5
    b: float = 0.75
    stem: bool = True


class RetrievalConfig(BaseModel):
    mode: Literal["dense", "bm25", "hybrid"] = "hybrid"
    top_k: int = 20
    rrf_k: int = 60  # Reciprocal Rank Fusion constant
    candidates_per_retriever: int = 50  # for hybrid: how deep each list is read before fusion
    bm25: BM25Config = Field(default_factory=BM25Config)
    translate_query: bool = False  # translate non-English queries to English with the LLM
    # one LLM call for every query: translate + split a multi-part question into sub-queries + Odoo terms;
    # each sub-query is searched and reranked on its own, the lists are fused with RRF (replaces translate_query)
    rewrite_query: bool = False
    max_subqueries: int = 3


class FilterConfig(BaseModel):
    enabled: bool = True
    min_score: Optional[float] = None  # threshold on the retriever score (dense cosine / fused)
    min_chars: int = 60
    dedup: bool = True
    near_dup_jaccard: float = 0.9
    max_per_doc: int = 3
    modules: list[str] = Field(default_factory=list)  # metadata filter, e.g. ["barcode"]


class RerankerConfig(BaseModel):
    # none | cross_encoder (HF, on your PC) | llm (Ollama) | bi_encoder (MiniLM rescoring) | maxsim (MiniLM token late interaction)
    backend: Literal["none", "cross_encoder", "llm", "bi_encoder", "maxsim"] = "none"
    model: Optional[str] = None
    top_n: int = 5
    min_score: Optional[float] = None  # threshold on reranker score; nothing left -> refusal
    batch_size: int = 16


class GenerationConfig(BaseModel):
    backend: Literal["ollama", "extractive"] = "ollama"
    model: Optional[str] = None  # default LLM_MODEL
    prompt: Literal["basic", "strict"] = "strict"
    max_context_chars: int = 6000
    answer_language: Literal["auto", "en", "ru"] = "auto"
    max_tokens: int = 700


class PipelineConfig(BaseModel):
    source: SourceConfig = Field(default_factory=SourceConfig)
    processing: ProcessingConfig = Field(default_factory=ProcessingConfig)
    chunking: ChunkingConfig = Field(default_factory=ChunkingConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    vectorstore: VectorStoreConfig = Field(default_factory=VectorStoreConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    reranker: RerankerConfig = Field(default_factory=RerankerConfig)
    generation: GenerationConfig = Field(default_factory=GenerationConfig)

    def with_overrides(self, overrides: dict[str, Any] | None) -> "PipelineConfig":
        if not overrides:
            return self
        data = deep_merge(self.model_dump(), overrides)
        return PipelineConfig.model_validate(data)

    def index_key(self) -> str:
        """Identity of an index: chunking + embedding (+ processing version)."""
        return f"p{self.processing.version}__{self.chunking.key()}__h{int(self.chunking.add_header)}__{self.embedding.key()}"

    def fingerprint(self) -> str:
        payload = json.dumps(self.model_dump(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]


def deep_merge(base: dict, overrides: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def dotted_to_nested(flat: dict[str, Any]) -> dict[str, Any]:
    """{"chunking.max_chars": 500} -> {"chunking": {"max_chars": 500}}"""
    nested: dict[str, Any] = {}
    for key, value in flat.items():
        cur = nested
        parts = key.split(".")
        for part in parts[:-1]:
            cur = cur.setdefault(part, {})
        cur[parts[-1]] = value
    return nested


def load_pipeline_config(path: Path | str | None = None, settings: Settings | None = None) -> PipelineConfig:
    settings = settings or Settings.from_env()
    path = Path(path) if path else settings.config_path
    if not path.exists():
        return PipelineConfig()
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    return PipelineConfig.model_validate(data)
