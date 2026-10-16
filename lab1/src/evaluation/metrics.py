"""Retrieval and answer metrics.

Retrieval metrics are computed on the *document ranking*: the ranked chunk list is reduced
to the order in which documents first appear (several chunks of one document count once).
This makes the numbers comparable between chunking strategies that produce different
numbers of chunks per document.

* Hit@K        - at least one relevant document in the top K;
* Recall@K     - share of the relevant documents found in the top K (matters for multi-doc);
* Precision@K  - share of the top K documents that are relevant;
* MRR          - 1 / rank of the first relevant document;
* nDCG@K       - ranking quality with binary relevance;
* EvidenceHit@K - a chunk in the top K *chunks* contains an evidence phrase, i.e. the
                  passage that actually answers the question was retrieved (stricter than
                  Hit@K: the right document with the wrong section does not count).
"""
from __future__ import annotations

import math
import statistics
from typing import Iterable

from evaluation.dataset import norm


def doc_ranking(doc_ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for d in doc_ids:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def hit_at_k(ranking: list[str], relevant: set[str], k: int) -> float:
    return float(any(d in relevant for d in ranking[:k]))


def recall_at_k(ranking: list[str], relevant: set[str], k: int) -> float:
    if not relevant:
        return 0.0
    return len(relevant & set(ranking[:k])) / len(relevant)


def precision_at_k(ranking: list[str], relevant: set[str], k: int) -> float:
    return len(relevant & set(ranking[:k])) / k


def mrr(ranking: list[str], relevant: set[str], k: int | None = None) -> float:
    for i, d in enumerate(ranking[:k] if k else ranking, start=1):
        if d in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(ranking: list[str], relevant: set[str], k: int) -> float:
    dcg = sum(1.0 / math.log2(i + 1) for i, d in enumerate(ranking[:k], start=1) if d in relevant)
    ideal = sum(1.0 / math.log2(i + 1) for i in range(1, min(len(relevant), k) + 1))
    return dcg / ideal if ideal else 0.0


def evidence_hit_at_k(chunk_texts: list[str], evidence: list[str], k: int) -> float:
    phrases = [norm(p) for p in evidence]
    for text in chunk_texts[:k]:
        t = norm(text)
        if any(p in t for p in phrases):
            return 1.0
    return 0.0


def retrieval_metrics(doc_ids: list[str], chunk_texts: list[str], relevant: list[str], evidence: list[str], ks: Iterable[int]) -> dict:
    """All metrics for one question and one ranked list of chunks."""
    rel = set(relevant)
    ranking = doc_ranking(doc_ids)
    out: dict[str, float] = {"mrr": mrr(ranking, rel)}
    for k in ks:
        out[f"hit@{k}"] = hit_at_k(ranking, rel, k)
        out[f"recall@{k}"] = recall_at_k(ranking, rel, k)
        out[f"precision@{k}"] = precision_at_k(ranking, rel, k)
        out[f"ndcg@{k}"] = ndcg_at_k(ranking, rel, k)
        out[f"evidence@{k}"] = evidence_hit_at_k(chunk_texts, evidence, k)
    return out


def keyword_coverage(answer: str, keywords: list[str]) -> float | None:
    """Share of keywords present in the answer; "a|b" accepts any alternative."""
    if not keywords:
        return None
    low = answer.lower()
    found = sum(1 for kw in keywords if any(alt.strip().lower() in low for alt in kw.split("|")))
    return found / len(keywords)


def mean(values: Iterable[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.mean(vals), 4) if vals else None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(round(p / 100 * (len(s) - 1)))))
    return round(s[idx], 1)


def aggregate(rows: list[dict], keys: Iterable[str]) -> dict:
    return {k: mean(r.get(k) for r in rows) for k in keys}


def best_threshold(pos_scores: list[float], neg_scores: list[float]) -> dict:
    """Threshold on a score that best separates answerable (pos) from no-answer (neg) questions.

    Below the threshold the pipeline refuses. Returns the threshold with the highest balanced
    accuracy = (share of answerable kept + share of no-answer refused) / 2.
    """
    if not pos_scores or not neg_scores:
        return {}
    candidates = sorted(set(pos_scores + neg_scores))
    best = None
    for t in candidates:
        kept = sum(1 for s in pos_scores if s >= t) / len(pos_scores)
        refused = sum(1 for s in neg_scores if s < t) / len(neg_scores)
        bal = (kept + refused) / 2
        if best is None or bal > best["balanced_accuracy"]:
            best = {"threshold": round(t, 4), "answerable_kept": round(kept, 4), "no_answer_refused": round(refused, 4), "balanced_accuracy": round(bal, 4)}
    return best
