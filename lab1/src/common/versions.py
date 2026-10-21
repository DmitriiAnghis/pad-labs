"""Exact versions of everything a result depends on, saved next to every experiment result.

A model *name* is not enough to repeat a run: an Ollama tag such as ``qwen3.5:4b`` can be
re-pulled as a different build, and a Hugging Face name points to whatever revision is newest.
So every result records:

* ``code``     - git commit of this repository (+ "dirty" when there are uncommitted changes);
* ``corpus``   - commit of the documentation repository from the grabber manifest;
* ``packages`` - versions of the libraries that change numbers (numpy, faiss, onnxruntime, ...);
* ``models``   - per model: Ollama digest + quantization, HF snapshot revision, or the sha256 of
                 the local ONNX file.

Everything is best effort: a missing tool gives ``null``, never an error.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path

from common.config import LAB_ROOT, PipelineConfig, Settings
from common.utils import read_json

log = logging.getLogger("versions")

PACKAGES = ("numpy", "scikit-learn", "faiss-cpu", "onnxruntime", "tokenizers", "snowballstemmer",
            "sentence-transformers", "torch", "transformers", "qdrant-client")


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=LAB_ROOT, capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def code_version() -> dict:
    commit = _git("rev-parse", "HEAD")
    status = _git("status", "--porcelain", "--untracked-files=no")
    return {"commit": commit, "dirty": bool(status) if status is not None else None}


def corpus_version(settings: Settings) -> dict | None:
    manifest = read_json(settings.raw_dir / "manifest.json", default=None) or {}
    last = manifest.get("last_sync") or {}
    commit = last.get("commit") or {}
    if not commit:
        return None
    return {"repo": (manifest.get("source") or {}).get("repo_url"), "branch": (manifest.get("source") or {}).get("branch"),
            "commit": commit.get("sha"), "date": commit.get("date")}


def package_versions() -> dict:
    out = {}
    for name in PACKAGES:
        try:
            out[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return out


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


@lru_cache(maxsize=8)
def _minilm_sha(models_dir: str) -> str | None:
    files = sorted(Path(models_dir).glob("**/model.onnx"))
    return _sha256_file(files[0]) if files else None


def hf_revision(name: str) -> str | None:
    """Snapshot revision of a Hugging Face model in the local cache (what was actually loaded)."""
    home = Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface")
    ref = home / "hub" / f"models--{name.replace('/', '--')}" / "refs" / "main"
    try:
        return ref.read_text().strip()
    except OSError:
        return None


def ollama_models(settings: Settings) -> dict[str, dict]:
    """tag -> {digest, parameter_size, quantization} from /api/tags; {} when Ollama is down."""
    import requests

    try:
        resp = requests.get(f"{settings.ollama_url}/api/tags", timeout=5)
        resp.raise_for_status()
    except requests.RequestException:
        return {}
    out = {}
    for m in resp.json().get("models", []):
        details = m.get("details") or {}
        out[m.get("name", "")] = {"digest": m.get("digest"), "parameter_size": details.get("parameter_size"),
                                  "quantization": details.get("quantization_level"), "modified_at": m.get("modified_at")}
    return out


def _ollama_entry(tag: str, tags: dict) -> dict:
    info = tags.get(tag) or tags.get(f"{tag}:latest")
    return {"provider": "ollama", "name": tag, **(info or {"digest": None})}


def model_versions(settings: Settings, config: PipelineConfig, judge: bool = False, ollama_tags: dict | None = None) -> dict:
    """The models this config really uses, with the exact build of each."""
    needs_ollama = (config.generation.backend == "ollama" or config.embedding.backend == "ollama"
                    or config.reranker.backend == "llm" or config.retrieval.translate_query or judge)
    tags = ollama_tags if ollama_tags is not None else (ollama_models(settings) if needs_ollama else {})
    out: dict[str, dict] = {}

    emb = config.embedding
    if emb.backend == "minilm":
        out["embedding"] = {"provider": "onnx", "name": "all-MiniLM-L6-v2", "sha256": _minilm_sha(str(settings.models_dir))}
    elif emb.backend == "ollama":
        out["embedding"] = _ollama_entry(emb.model or settings.embed_model, tags)
    else:
        out["embedding"] = {"provider": "local", "name": emb.key()}

    rr = config.reranker
    if rr.backend == "cross_encoder":
        name = rr.model or settings.rerank_model
        out["reranker"] = {"provider": "huggingface", "name": name, "revision": hf_revision(name)}
    elif rr.backend == "llm":
        out["reranker"] = _ollama_entry(rr.model or settings.llm_model, tags)
    elif rr.backend in ("maxsim", "bi_encoder"):
        out["reranker"] = {"provider": "onnx", "name": f"{rr.backend}:all-MiniLM-L6-v2", "sha256": _minilm_sha(str(settings.models_dir))}

    llm = config.generation.model or settings.llm_model
    if config.generation.backend == "ollama":
        out["llm"] = {**_ollama_entry(llm, tags), "temperature": settings.llm_temperature, "num_ctx": settings.llm_num_ctx}
    else:
        out["llm"] = {"provider": "local", "name": config.generation.backend}
    if config.retrieval.translate_query:
        out["translator"] = _ollama_entry(llm, tags)
    if judge:
        out["judge"] = _ollama_entry(settings.judge_model, tags)
    return out


def environment(settings: Settings) -> dict:
    return {"code": code_version(), "corpus": corpus_version(settings), "packages": package_versions()}
