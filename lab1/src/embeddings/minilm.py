"""all-MiniLM-L6-v2 (sentence-transformers) in ONNX format, run with onnxruntime on CPU.

* 384-dim vectors, English, max 256 word-piece tokens (longer input is truncated!), 22M params.
* Weights: the ONNX export distributed by Chroma (public S3 bucket) - no Hugging Face account,
  no PyTorch, ~80 MB download, ~0.3 s per 32 chunks on a laptop CPU.
* Also exposes token-level vectors, used by the "maxsim" late-interaction reranker experiment.
"""
from __future__ import annotations

import logging
import tarfile
from pathlib import Path

import numpy as np
import requests

from embeddings.base import Embedder, EmbeddingError, l2_normalize

log = logging.getLogger("embeddings.minilm")

MINILM_URL = "https://chroma-onnx-models.s3.amazonaws.com/all-MiniLM-L6-v2/onnx.tar.gz"


def download_minilm(models_dir: Path, url: str = MINILM_URL, timeout: int = 300) -> Path:
    target = models_dir / "all-MiniLM-L6-v2"
    if (target / "onnx" / "model.onnx").exists():
        return target / "onnx"
    target.mkdir(parents=True, exist_ok=True)
    archive = target / "onnx.tar.gz"
    log.info("downloading MiniLM ONNX from %s", url)
    try:
        with requests.get(url, stream=True, timeout=timeout) as resp:
            resp.raise_for_status()
            with open(archive, "wb") as fh:
                for block in resp.iter_content(1 << 20):
                    fh.write(block)
    except requests.RequestException as exc:
        raise EmbeddingError(f"cannot download MiniLM model from {url}: {exc}") from exc
    with tarfile.open(archive) as tar:
        members = [m for m in tar.getmembers() if m.isfile() and not Path(m.name).is_absolute() and ".." not in Path(m.name).parts]
        tar.extractall(target, members=members)
    archive.unlink(missing_ok=True)
    return target / "onnx"


class MiniLMEmbedder(Embedder):
    max_tokens = 256

    def __init__(self, models_dir: Path, max_length: int = 256, threads: int = 0) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        model_dir = download_minilm(models_dir)
        self.name = "all-MiniLM-L6-v2"
        self.dim = 384
        self.max_length = max_length
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=max_length)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        opts = ort.SessionOptions()
        if threads:
            opts.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}
        self.truncated = 0  # how many inputs were longer than max_length tokens

    def _run(self, texts: list[str]) -> tuple[np.ndarray, np.ndarray]:
        enc = self.tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in enc], dtype=np.int64)
        mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
        self.truncated += sum(1 for e in enc if e.overflowing)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        token_emb = self.session.run(None, feeds)[0]  # (batch, seq, 384)
        return token_emb, mask

    def embed_documents(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        out = []
        for i in range(0, len(texts), batch_size):
            token_emb, mask = self._run(texts[i:i + batch_size])
            m = mask[..., None].astype(np.float32)
            pooled = (token_emb * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None)  # mean pooling
            out.append(l2_normalize(pooled))
        return np.concatenate(out) if out else np.zeros((0, self.dim), np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed_documents([text])[0]

    def token_vectors(self, texts: list[str]) -> list[np.ndarray]:
        """Normalised per-token vectors (without padding), for late-interaction scoring."""
        token_emb, mask = self._run(texts)
        result = []
        for emb, m in zip(token_emb, mask):
            result.append(l2_normalize(emb[m.astype(bool)]))
        return result
