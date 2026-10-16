"""Okapi BM25 - lexical ranking ("find the chunks that contain the query words").

score(q, d) = sum over query terms t of  idf(t) * tf(t,d) * (k1 + 1) / (tf(t,d) + k1 * (1 - b + b * |d| / avgdl))

* tf saturation (k1): the 10th occurrence of a word adds less than the 2nd;
* length normalisation (b): a long chunk does not win just because it has more words;
* idf: rare words ("FEFO", "putaway") weigh more than frequent ones ("product").
Strong at exact terms, codes and UI labels; blind to synonyms and other languages.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict

import numpy as np

from retrieval.tokenize import tokenize


class BM25Index:
    def __init__(self, texts: list[str], k1: float = 1.5, b: float = 0.75, stem: bool = True) -> None:
        self.k1 = k1
        self.b = b
        self.stem = stem
        self.n_docs = len(texts)
        self.doc_len = np.zeros(self.n_docs, dtype=np.float32)
        postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, text in enumerate(texts):
            toks = tokenize(text, stem=stem)
            self.doc_len[i] = len(toks)
            for term, tf in Counter(toks).items():
                postings[term].append((i, tf))
        self.avgdl = float(self.doc_len.mean()) if self.n_docs else 0.0
        self.postings: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        self.idf: dict[str, float] = {}
        for term, plist in postings.items():
            ids = np.array([p[0] for p in plist], dtype=np.int32)
            tfs = np.array([p[1] for p in plist], dtype=np.float32)
            self.postings[term] = (ids, tfs)
            df = len(plist)
            self.idf[term] = math.log(1 + (self.n_docs - df + 0.5) / (df + 0.5))

    def scores(self, query: str) -> np.ndarray:
        scores = np.zeros(self.n_docs, dtype=np.float32)
        if not self.n_docs:
            return scores
        norm = self.k1 * (1 - self.b + self.b * self.doc_len / max(self.avgdl, 1e-9))
        for term in set(tokenize(query, stem=self.stem)):
            if term not in self.postings:
                continue
            ids, tfs = self.postings[term]
            scores[ids] += self.idf[term] * tfs * (self.k1 + 1) / (tfs + norm[ids])
        return scores

    def search(self, query: str, k: int) -> list[tuple[int, float]]:
        scores = self.scores(query)
        if k >= len(scores):
            order = np.argsort(-scores)
        else:
            top = np.argpartition(-scores, k)[:k]
            order = top[np.argsort(-scores[top])]
        return [(int(i), float(scores[i])) for i in order if scores[i] > 0]
