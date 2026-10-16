"""TF-IDF + LSA (truncated SVD): classic pre-neural "semantic" vectors, the baseline embedder.

Fitted on the chunk texts of the current index. LSA compresses the sparse TF-IDF matrix into
``dim`` latent topics, so documents that share related (not identical) words get close.
Fast, deterministic, no model download - but knows nothing beyond our corpus and cannot
match a Russian question to English text.
"""
from __future__ import annotations

import numpy as np
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

from embeddings.base import Embedder, l2_normalize
from retrieval.tokenize import tokenize


def _analyzer(text: str) -> list[str]:
    toks = tokenize(text)
    return toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:])]  # unigrams + bigrams


class TfidfLsaEmbedder(Embedder):
    needs_fit = True

    def __init__(self, dim: int = 256, seed: int = 42) -> None:
        self.name = f"tfidf-lsa{dim}"
        self.target_dim = dim
        self.dim = dim
        self.seed = seed
        self.vectorizer: TfidfVectorizer | None = None
        self.svd: TruncatedSVD | None = None

    def fit(self, texts: list[str]) -> None:
        self.vectorizer = TfidfVectorizer(analyzer=_analyzer, sublinear_tf=True, min_df=1, max_df=0.9)
        matrix = self.vectorizer.fit_transform(texts)
        n_comp = max(2, min(self.target_dim, matrix.shape[1] - 1, matrix.shape[0] - 1))
        self.svd = TruncatedSVD(n_components=n_comp, random_state=self.seed)
        self.svd.fit(matrix)
        self.dim = n_comp

    def _transform(self, texts: list[str]) -> np.ndarray:
        if self.vectorizer is None or self.svd is None:
            raise RuntimeError("TF-IDF embedder is not fitted")
        return l2_normalize(self.svd.transform(self.vectorizer.transform(texts)))

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._transform(texts)

    def embed_query(self, text: str) -> np.ndarray:
        return self._transform([text])[0]
