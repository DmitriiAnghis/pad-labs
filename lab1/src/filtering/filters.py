"""Context filters between the retriever and the LLM.

Order (each step keeps the ranking order and records why a candidate was dropped):
1. metadata   - keep only chunks of selected modules (e.g. only "barcode");
2. min_chars  - drop fragments too short to contain an answer;
3. min_score  - similarity threshold (dense cosine when available, otherwise the retriever score);
                if *everything* is below the threshold the question is probably out of the corpus;
4. dedup      - exact duplicates (same text) and near duplicates (word Jaccard >= threshold),
                e.g. overlapping windows of fixed_overlap chunking;
5. max_per_doc - at most N chunks from one document, so the context covers several sources.
"""
from __future__ import annotations

import re

from common.config import FilterConfig
from retrieval.retriever import Candidate

_WORD = re.compile(r"\w+")


def _shingles(text: str) -> set[str]:
    return set(w.lower() for w in _WORD.findall(text))


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def score_for_threshold(c: Candidate) -> float:
    return float(c.scores.get("dense", c.score))


def apply_filters(cands: list[Candidate], cfg: FilterConfig) -> tuple[list[Candidate], list[dict]]:
    if not cfg.enabled:
        return list(cands), []
    kept: list[Candidate] = []
    dropped: list[dict] = []
    seen_text: set[str] = set()
    kept_shingles: list[set] = []
    per_doc: dict[str, int] = {}

    def drop(c: Candidate, reason: str) -> None:
        dropped.append({"chunk_id": c.chunk["chunk_id"], "doc_id": c.chunk["doc_id"], "rank": c.rank, "reason": reason})

    for c in cands:
        ch = c.chunk
        if cfg.modules and ch.get("module") not in cfg.modules:
            drop(c, f"module {ch.get('module')} not in {cfg.modules}")
            continue
        if ch["char_count"] < cfg.min_chars:
            drop(c, f"too short ({ch['char_count']} < {cfg.min_chars} chars)")
            continue
        if cfg.min_score is not None and score_for_threshold(c) < cfg.min_score:
            drop(c, f"score {score_for_threshold(c):.3f} < {cfg.min_score}")
            continue
        if cfg.dedup:
            norm = " ".join(ch["text"].lower().split())
            if norm in seen_text:
                drop(c, "exact duplicate")
                continue
            sh = _shingles(ch["text"])
            if any(jaccard(sh, other) >= cfg.near_dup_jaccard for other in kept_shingles):
                drop(c, f"near duplicate (jaccard >= {cfg.near_dup_jaccard})")
                continue
            seen_text.add(norm)
            kept_shingles.append(sh)
        if cfg.max_per_doc and per_doc.get(ch["doc_id"], 0) >= cfg.max_per_doc:
            drop(c, f"more than {cfg.max_per_doc} chunks from one document")
            continue
        per_doc[ch["doc_id"]] = per_doc.get(ch["doc_id"], 0) + 1
        kept.append(c)
    return kept, dropped
