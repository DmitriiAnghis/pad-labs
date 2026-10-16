"""Retrieval evaluation: every question goes through retrieve() (no LLM), and the ranked lists
of three stages are scored against the labels:

* ``retriever`` - Top-K straight from the retriever;
* ``filtered``  - after the context filters;
* ``final``     - after the reranker, the Top-N chunks that would be sent to the LLM.

Besides ranking quality it records two things the RAG needs for refusals:
* how often nothing is left for the LLM (answerable vs no-answer questions);
* the best score threshold separating answerable from no-answer questions
  (top dense cosine of the retriever, top reranker score).
"""
from __future__ import annotations

import logging
from typing import Iterable

from common.config import PipelineConfig, Settings
from common.utils import utc_now, write_json
from evaluation.dataset import load_questions, question_text
from evaluation.metrics import aggregate, best_threshold, mean, percentile, retrieval_metrics

log = logging.getLogger("evaluation.retrieval")

STAGES = ("retriever", "filtered", "final")
DEFAULT_KS = (1, 3, 5, 10, 20)
# the numbers shown in summaries / reports
HEADLINE = ("hit@1", "hit@5", "recall@5", "mrr", "ndcg@5", "evidence@5")


def evaluate_retrieval(pipeline, questions: list[dict], lang: str = "en", ks: Iterable[int] = DEFAULT_KS) -> dict:
    ks = tuple(ks)
    rows: list[dict] = []
    for q in questions:
        text = question_text(q, lang)
        r = pipeline.retrieve(text)
        row = {
            "id": q["id"],
            "type": q["type"],
            "lang": lang,
            "question": text,
            "search_query": r.search_query if r.search_query != text else None,
            "ms": round(sum(r.timings_ms.values()), 1),
            "timings_ms": {k: round(v, 1) for k, v in r.timings_ms.items()},
            "context_empty": not r.reranked,
            "top_dense": max((c.scores["dense"] for c in r.retrieved if "dense" in c.scores), default=None),
            "top_rerank": r.reranked[0].scores.get("rerank") if r.reranked else None,
            "final_docs": [c.chunk["doc_id"] for c in r.reranked],
        }
        if q["type"] != "no_answer":
            for stage, cands in (("retriever", r.retrieved), ("filtered", r.filtered), ("final", r.reranked)):
                row[stage] = retrieval_metrics(
                    [c.chunk["doc_id"] for c in cands],
                    [c.chunk["text"] for c in cands],
                    q["relevant_docs"],
                    q["evidence"],
                    ks,
                )
        rows.append(row)
    return {"summary": summarize(rows, ks), "rows": rows}


def summarize(rows: list[dict], ks: Iterable[int] = DEFAULT_KS) -> dict:
    ks = tuple(ks)
    answerable = [r for r in rows if r["type"] != "no_answer"]
    no_answer = [r for r in rows if r["type"] == "no_answer"]
    metric_keys = ["mrr"] + [f"{m}@{k}" for k in ks for m in ("hit", "recall", "precision", "ndcg", "evidence")]
    summary: dict = {"questions": len(rows), "answerable": len(answerable), "no_answer": len(no_answer)}
    for stage in STAGES:
        summary[stage] = aggregate([r[stage] for r in answerable], metric_keys)
    by_type = {}
    for t in sorted({r["type"] for r in answerable}):
        sub = [r["final"] for r in answerable if r["type"] == t]
        by_type[t] = {"n": len(sub), **aggregate(sub, HEADLINE)}
    summary["final_by_type"] = by_type
    summary["refusal"] = {
        # nothing left after filters/reranker -> the pipeline refuses without calling the LLM
        "answerable_without_context": mean(float(r["context_empty"]) for r in answerable),
        "no_answer_without_context": mean(float(r["context_empty"]) for r in no_answer),
        "threshold_top_dense": best_threshold(
            [r["top_dense"] for r in answerable if r["top_dense"] is not None],
            [r["top_dense"] for r in no_answer if r["top_dense"] is not None],
        ),
        "threshold_top_rerank": best_threshold(
            [r["top_rerank"] for r in answerable if r["top_rerank"] is not None],
            [r["top_rerank"] for r in no_answer if r["top_rerank"] is not None],
        ),
    }
    lat = [r["ms"] for r in rows]
    summary["latency_ms"] = {"mean": mean(lat), "p50": percentile(lat, 50), "p95": percentile(lat, 95)}
    return summary


def run_retrieval_eval(settings: Settings, config: PipelineConfig, lang: str = "en", ks: Iterable[int] = DEFAULT_KS, limit: int | None = None) -> dict:
    from pipeline.rag import RagPipeline

    questions = load_questions(settings.eval_dir / "questions.jsonl", limit=limit)
    pipeline = RagPipeline.from_config(settings, config)
    result = evaluate_retrieval(pipeline, questions, lang, ks)
    path = settings.runs_dir / f"retrieval_{lang}_{utc_now().replace(':', '').replace('-', '')[:15]}.json"
    write_json(path, {"config": config.model_dump(), "index": pipeline.index.meta, **result})
    result["path"] = str(path)
    return result
