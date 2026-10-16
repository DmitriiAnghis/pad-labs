import math

from evaluation.metrics import (best_threshold, doc_ranking, evidence_hit_at_k, hit_at_k, keyword_coverage, mrr,
                                ndcg_at_k, precision_at_k, recall_at_k, retrieval_metrics)


def test_doc_ranking_deduplicates_in_order():
    assert doc_ranking(["a", "b", "a", "c", "b"]) == ["a", "b", "c"]


def test_ranking_metrics():
    ranking = ["x", "a", "y", "b"]
    rel = {"a", "b"}
    assert hit_at_k(ranking, rel, 1) == 0.0 and hit_at_k(ranking, rel, 2) == 1.0
    assert recall_at_k(ranking, rel, 2) == 0.5 and recall_at_k(ranking, rel, 4) == 1.0
    assert precision_at_k(ranking, rel, 4) == 0.5
    assert mrr(ranking, rel) == 0.5
    ideal = 1 + 1 / math.log2(3)
    assert math.isclose(ndcg_at_k(ranking, rel, 4), (1 / math.log2(3) + 1 / math.log2(5)) / ideal)
    assert ndcg_at_k(["a", "b"], rel, 2) == 1.0


def test_evidence_hit_is_case_and_space_insensitive():
    texts = ["nothing here", "Odoo has a default 100-millisecond\n delay between scans"]
    assert evidence_hit_at_k(texts, ["default 100-millisecond delay"], 2) == 1.0
    assert evidence_hit_at_k(texts, ["default 100-millisecond delay"], 1) == 0.0


def test_chunk_level_duplicates_count_once():
    m = retrieval_metrics(["a", "a", "a", "b"], ["t"] * 4, ["b"], ["zzz"], [2])
    assert m["hit@2"] == 1.0  # documents, not chunks: "b" is the 2nd document


def test_keyword_coverage_with_alternatives():
    assert keyword_coverage("Set it to 5 characters", ["five|5"]) == 1.0
    assert keyword_coverage("FIFO and LIFO", ["fifo", "fefo"]) == 0.5
    assert keyword_coverage("anything", []) is None


def test_best_threshold_separates_scores():
    res = best_threshold([0.8, 0.7, 0.65], [0.3, 0.5])
    assert res["balanced_accuracy"] == 1.0
    assert 0.5 < res["threshold"] <= 0.65
