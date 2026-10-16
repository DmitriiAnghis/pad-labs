"""Evaluation dataset: questions with reference answers and relevance labels.

``data/eval/questions.jsonl``, one question per line:

    id, type             factoid | specific | multi_doc | contextual | no_answer
    question_en/_ru      the same question in two languages
    expected_answer      reference answer (for the LLM judge and for people)
    relevant_docs        doc_ids that answer the question (empty for no_answer)
    evidence             short verbatim phrases (lowercase) from the relevant documents;
                         a retrieved chunk that contains one of them holds the answer
    answer_keywords      words the answer must contain; "a|b" means a or b

Relevance is labelled per *document* and per *evidence phrase*, not per chunk id. Chunk ids
change with every chunking configuration, so chunk-level labels (as in the reference
project) would let us compare only one chunking strategy; document + evidence labels work
for all of them.
"""
from __future__ import annotations

import re
from pathlib import Path

from common.utils import read_jsonl

TYPES = ("factoid", "specific", "multi_doc", "contextual", "no_answer")
LANGS = ("en", "ru")

_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    """Lowercase + collapsed whitespace: the form evidence phrases are matched in."""
    return _WS.sub(" ", text.lower()).strip()


def load_questions(path: Path | str, types: list[str] | None = None, limit: int | None = None) -> list[dict]:
    questions = read_jsonl(Path(path))
    if types:
        questions = [q for q in questions if q["type"] in types]
    if limit:
        questions = questions[:limit]
    return questions


def question_text(q: dict, lang: str) -> str:
    return q.get(f"question_{lang}") or q["question_en"]


def validate_dataset(questions: list[dict], docs: list[dict]) -> list[str]:
    """Returns a list of problems (empty = dataset is consistent with the processed corpus)."""
    from preprocessing.documents import document_text

    texts = {d["doc_id"]: norm(document_text(d)) for d in docs}
    errors: list[str] = []
    seen: set[str] = set()
    for q in questions:
        qid = q.get("id", "?")
        if qid in seen:
            errors.append(f"{qid}: duplicate id")
        seen.add(qid)
        if q.get("type") not in TYPES:
            errors.append(f"{qid}: unknown type {q.get('type')}")
        for lang in LANGS:
            if not q.get(f"question_{lang}"):
                errors.append(f"{qid}: missing question_{lang}")
        if q["type"] == "no_answer":
            if q["relevant_docs"] or q["evidence"]:
                errors.append(f"{qid}: no_answer question must have no relevant docs/evidence")
            continue
        if not q["relevant_docs"]:
            errors.append(f"{qid}: no relevant_docs")
        for doc_id in q["relevant_docs"]:
            if doc_id not in texts:
                errors.append(f"{qid}: relevant doc {doc_id} is not in the corpus")
        if not q["evidence"]:
            errors.append(f"{qid}: no evidence phrases")
        for phrase in q["evidence"]:
            p = norm(phrase)
            if not any(p in texts.get(d, "") for d in q["relevant_docs"]):
                where = [d for d, t in texts.items() if p in t]
                errors.append(f"{qid}: evidence '{phrase}' not found in its relevant docs (found in: {where or 'nowhere'})")
    return errors
