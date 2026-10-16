"""The evaluation set must stay consistent with the processed corpus."""
import pytest

from common.config import Settings
from evaluation.dataset import TYPES, load_questions, validate_dataset
from preprocessing.documents import load_documents

S = Settings.from_env()
QUESTIONS = S.eval_dir / "questions.jsonl"


def test_dataset_shape():
    qs = load_questions(QUESTIONS)
    assert len(qs) >= 30
    counts = {t: sum(q["type"] == t for q in qs) for t in TYPES}
    assert all(counts[t] >= 5 for t in TYPES), counts


@pytest.mark.skipif(not (S.processed_dir / "docs.jsonl").exists(), reason="run 'python run.py grab' and 'process' first")
def test_every_evidence_phrase_is_in_its_relevant_documents():
    errors = validate_dataset(load_questions(QUESTIONS), load_documents(S))
    assert errors == []
