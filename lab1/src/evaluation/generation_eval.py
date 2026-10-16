"""Answer evaluation: the full pipeline (retrieval -> LLM) on every question.

Automatic metrics (no extra model needed):
* behaviour accuracy - answers answerable questions and refuses no-answer ones;
* false refusals     - refused although the answer is in the corpus;
* keyword coverage   - share of the expected key facts present in the answer;
* citation rate      - answer cites at least one passage [n];
* citation precision - share of cited passages that come from a relevant document;
* latency            - total and LLM time per question.

Optional LLM-as-judge (``--judge``, model JUDGE_MODEL): correctness 0-2 against the reference
answer and faithfulness 0/1 against the retrieved context. A judge from the same model family
is biased, so its numbers are only compared between variants, never read as absolute truth.
"""
from __future__ import annotations

import json
import logging
import re

from common.config import PipelineConfig, Settings
from common.utils import utc_now, write_json
from evaluation.dataset import load_questions, question_text
from evaluation.metrics import keyword_coverage, mean, percentile

log = logging.getLogger("evaluation.generation")

JUDGE_PROMPT = """You are grading an answer of a documentation assistant for the Odoo Inventory app.

Question: {question}

Reference answer: {reference}

Retrieved context given to the assistant:
{context}

Assistant answer:
{answer}

Grade the assistant answer:
- "correct": 2 if it contains the key facts of the reference answer, 1 if partially, 0 if wrong or missing.
- "faithful": 1 if every statement is supported by the retrieved context, 0 if it adds unsupported facts.
Reply with JSON only: {{"correct": 0|1|2, "faithful": 0|1, "reason": "<one short sentence>"}}"""


class Judge:
    def __init__(self, settings: Settings, model: str | None = None) -> None:
        from generation.ollama_client import OllamaClient

        self.client = OllamaClient(settings)
        self.model = model or settings.judge_model

    def grade(self, question: str, reference: str, context: str, answer: str) -> dict:
        prompt = JUDGE_PROMPT.format(question=question, reference=reference, context=context[:6000], answer=answer)
        reply = self.client.chat(self.model, [{"role": "user", "content": prompt}], max_tokens=200, temperature=0.0, json_format=True)
        try:
            data = json.loads(reply["text"])
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", reply["text"], re.S)
            data = json.loads(m.group(0)) if m else {}
        correct = data.get("correct")
        faithful = data.get("faithful")
        return {
            "correct": float(correct) / 2 if isinstance(correct, (int, float)) else None,
            "faithful": float(faithful) if isinstance(faithful, (int, float)) else None,
            "reason": data.get("reason"),
        }


def evaluate_generation(pipeline, questions: list[dict], lang: str = "en", judge: Judge | None = None) -> dict:
    from generation.generator import build_context

    rows: list[dict] = []
    for q in questions:
        text = question_text(q, lang)
        ans = pipeline.answer(text)
        should_refuse = q["type"] == "no_answer"
        relevant = set(q["relevant_docs"])
        cited = [s for s in ans.sources if s["cited"]]
        row = {
            "id": q["id"],
            "type": q["type"],
            "lang": lang,
            "question": text,
            "answer": ans.text,
            "refused": ans.refused,
            "refusal_reason": ans.refusal_reason,
            "correct_behaviour": ans.refused == should_refuse,
            "model": ans.model,
            "ms": round(ans.timings_ms.get("total", 0.0), 1),
            "generate_ms": round(ans.timings_ms.get("generate", 0.0), 1),
            "sources": [{"n": s["n"], "doc_id": s["doc_id"], "cited": s["cited"]} for s in ans.sources],
        }
        if not should_refuse and not ans.refused:
            row["keyword_coverage"] = keyword_coverage(ans.text, q["answer_keywords"])
            row["has_citation"] = bool(cited)
            row["citation_precision"] = (sum(1 for s in cited if s["doc_id"] in relevant) / len(cited)) if cited else None
        if judge is not None and not should_refuse and not ans.refused:
            context, _ = build_context(ans.retrieval.reranked, pipeline.config.generation.max_context_chars)
            try:
                row["judge"] = judge.grade(text, q["expected_answer"], context, ans.text)
            except Exception as exc:  # judge failure must not stop the evaluation
                log.warning("judge failed on %s: %s", q["id"], exc)
                row["judge"] = {"correct": None, "faithful": None, "reason": f"judge error: {exc}"}
        rows.append(row)
    return {"summary": summarize(rows), "rows": rows}


def summarize(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["type"] != "no_answer"]
    no_answer = [r for r in rows if r["type"] == "no_answer"]
    answered = [r for r in answerable if not r["refused"]]
    lat = [r["ms"] for r in rows]
    gen = [r["generate_ms"] for r in rows if r["generate_ms"]]
    summary = {
        "questions": len(rows),
        "behaviour_accuracy": mean(float(r["correct_behaviour"]) for r in rows),
        "no_answer_refused": mean(float(r["refused"]) for r in no_answer),
        "false_refusals": mean(float(r["refused"]) for r in answerable),
        "keyword_coverage": mean(r.get("keyword_coverage") for r in answered),
        "citation_rate": mean(float(r["has_citation"]) for r in answered if "has_citation" in r),
        "citation_precision": mean(r.get("citation_precision") for r in answered),
        "latency_ms": {"mean": mean(lat), "p50": percentile(lat, 50), "p95": percentile(lat, 95)},
        "generate_ms": {"mean": mean(gen), "p50": percentile(gen, 50), "p95": percentile(gen, 95)},
    }
    judged = [r["judge"] for r in answered if r.get("judge")]
    if judged:
        summary["judge_correct"] = mean(j["correct"] for j in judged)
        summary["judge_faithful"] = mean(j["faithful"] for j in judged)
    by_type = {}
    for t in sorted({r["type"] for r in rows}):
        sub = [r for r in rows if r["type"] == t]
        by_type[t] = {
            "n": len(sub),
            "behaviour_accuracy": mean(float(r["correct_behaviour"]) for r in sub),
            "keyword_coverage": mean(r.get("keyword_coverage") for r in sub),
        }
    summary["by_type"] = by_type
    return summary


def run_generation_eval(settings: Settings, config: PipelineConfig, lang: str = "en", judge: bool = False, limit: int | None = None) -> dict:
    from pipeline.rag import RagPipeline

    questions = load_questions(settings.eval_dir / "questions.jsonl", limit=limit)
    pipeline = RagPipeline.from_config(settings, config)
    result = evaluate_generation(pipeline, questions, lang, Judge(settings) if judge else None)
    path = settings.runs_dir / f"generation_{lang}_{utc_now().replace(':', '').replace('-', '')[:15]}.json"
    write_json(path, {"config": config.model_dump(), **result})
    result["path"] = str(path)
    return result
