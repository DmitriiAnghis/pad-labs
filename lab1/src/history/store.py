"""Answer history and manual ratings (append-only JSONL files in data/history).

    answers.jsonl   one line per answer: question, answer, sources, model, prompt, config
    ratings.jsonl   one line per rating: answer_id, score 1-5, flags, comment (latest wins)

Two sources of answers:
* ``api``        - every /ask answer (the person rates it in the web UI or via the API);
* ``eval:<run>`` - answers of a generation run on the fixed question set (``run.py rate``),
                   so the same questions get a manual grade next to the automatic metrics.

Flags name *what* is wrong, the score says *how bad*; the summary counts both per model/prompt.
The files are never truncated (unlike a capped history table): rated answers are research data.
Questions are stored in plain text, so data/history is in .gitignore.
"""
from __future__ import annotations

import json
import threading
import uuid
from collections import defaultdict
from pathlib import Path

from common.utils import read_jsonl, utc_now
from evaluation.metrics import mean

FLAGS = {
    "factual_error": "a statement contradicts the documentation",
    "unsupported": "a statement is not in the cited passages (invented)",
    "incomplete": "a required step or fact is missing",
    "irrelevant": "answers another question",
    "wrong_refusal": "refused although the answer is in the documentation, or answered although it is not",
    "bad_citation": "citations point to passages that do not support the sentence",
}


class HistoryError(ValueError):
    pass


class AnswerHistory:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.answers_path = self.dir / "answers.jsonl"
        self.ratings_path = self.dir / "ratings.jsonl"
        self._lock = threading.Lock()

    def _append(self, path: Path, record: dict) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ answers
    def add_answer(self, record: dict) -> str:
        record = {"answer_id": record.get("answer_id") or uuid.uuid4().hex[:16], "created_at": utc_now(), **record}
        self._append(self.answers_path, record)
        return record["answer_id"]

    def answers(self) -> list[dict]:
        return read_jsonl(self.answers_path)

    def get(self, answer_id: str) -> dict | None:
        return next((a for a in self.answers() if a["answer_id"] == answer_id), None)

    # ------------------------------------------------------------------ ratings
    def rate(self, answer_id: str, score: int, flags: list[str] | None = None, comment: str = "", rater: str = "") -> dict:
        flags = sorted(set(flags or []))
        unknown = [f for f in flags if f not in FLAGS]
        if unknown:
            raise HistoryError(f"unknown flags {unknown}; allowed: {sorted(FLAGS)}")
        if not 1 <= int(score) <= 5:
            raise HistoryError("score must be 1..5")
        if self.get(answer_id) is None:
            raise HistoryError(f"answer {answer_id} not found")
        rating = {"answer_id": answer_id, "score": int(score), "flags": flags, "comment": comment, "rater": rater, "rated_at": utc_now()}
        self._append(self.ratings_path, rating)
        return rating

    def ratings(self) -> dict[str, dict]:
        """answer_id -> latest rating."""
        return {r["answer_id"]: r for r in read_jsonl(self.ratings_path)}

    def recent(self, limit: int = 20) -> list[dict]:
        ratings = self.ratings()
        rows = self.answers()[-limit:][::-1]
        return [{**a, "rating": ratings.get(a["answer_id"])} for a in rows]

    def summary(self) -> dict:
        """Per (source, model, prompt): answers, rated, mean score, share of every flag."""
        ratings = self.ratings()
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for a in self.answers():
            groups[(a.get("source", "api"), a.get("model", ""), a.get("prompt", ""))].append(a)
        out = []
        for (source, model, prompt), items in sorted(groups.items()):
            rated = [ratings[a["answer_id"]] for a in items if a["answer_id"] in ratings]
            out.append({
                "source": source,
                "model": model,
                "prompt": prompt,
                "answers": len(items),
                "rated": len(rated),
                "mean_score": mean(r["score"] for r in rated),
                "flags": {f: mean(float(f in r["flags"]) for r in rated) for f in FLAGS} if rated else {},
            })
        return {"groups": out, "flags": FLAGS}
