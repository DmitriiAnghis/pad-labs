from common.config import FilterConfig
from filtering.filters import apply_filters, jaccard
from retrieval.bm25 import BM25Index
from retrieval.retriever import Candidate
from retrieval.tokenize import tokenize


def test_tokenize_stems_and_drops_stopwords():
    assert tokenize("The pickings are picked") == tokenize("picking pick")
    assert "the" not in tokenize("the wave")


def test_bm25_ranks_matching_document_first():
    texts = ["wave transfers split pickings", "FEFO removal strategy expiration", "RFID tags SGTIN-96"]
    bm25 = BM25Index(texts)
    assert bm25.search("expiration date removal", 3)[0][0] == 1
    assert bm25.search("sgtin-96", 1)[0][0] == 2
    assert bm25.search("payroll", 3) == [] or all(s == 0 for _, s in bm25.search("payroll", 3))


def cand(i, doc, text, dense=0.8):
    chunk = {"chunk_id": f"c{i}", "doc_id": doc, "text": text, "char_count": len(text), "module": doc.split("/")[0]}
    return Candidate(i, chunk, dense, i + 1, "dense", {"dense": dense})


def test_filters_drop_short_duplicates_and_limit_per_doc():
    long = "a sufficiently long passage about wave transfers and batch pickings in the warehouse"
    cands = [
        cand(0, "inventory/a", long),
        cand(1, "inventory/a", long),  # exact duplicate
        cand(2, "inventory/b", "short"),  # too short
        cand(3, "inventory/a", long + " again"),
        cand(4, "inventory/a", "another long passage about something else entirely, removal strategies"),
        cand(5, "barcode/c", "barcode passage long enough to be kept by the length filter ok"),
    ]
    kept, dropped = apply_filters(cands, FilterConfig(min_chars=20, near_dup_jaccard=0.7, max_per_doc=1))
    assert [c.chunk["chunk_id"] for c in kept] == ["c0", "c5"]
    reasons = {d["chunk_id"]: d["reason"] for d in dropped}
    assert reasons["c1"] == "exact duplicate"
    assert reasons["c2"].startswith("too short")
    assert reasons["c3"].startswith("near duplicate")
    assert "more than 1" in reasons["c4"]


def test_score_threshold_and_module_filter():
    text = "a sufficiently long passage for the filters to keep it in place"
    cands = [cand(0, "inventory/a", text, 0.7), cand(1, "barcode/b", text + " b", 0.3)]
    kept, _ = apply_filters(cands, FilterConfig(min_score=0.5, dedup=False))
    assert [c.idx for c in kept] == [0]
    kept, _ = apply_filters(cands, FilterConfig(modules=["barcode"], dedup=False))
    assert [c.idx for c in kept] == [1]


def test_jaccard():
    assert jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert jaccard(set(), {"a"}) == 0.0
