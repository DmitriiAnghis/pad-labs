import pytest

from common.config import ChunkingConfig
from preprocessing.chunking import chunk_corpus, chunk_document, split_long_text


@pytest.mark.parametrize("strategy", ["fixed", "fixed_overlap", "paragraph", "section"])
def test_every_strategy_respects_size_and_keeps_metadata(strategy, small_corpus):
    cfg = ChunkingConfig(strategy=strategy, max_chars=120, overlap=30, min_chars=20)
    chunks = chunk_corpus(small_corpus, cfg)
    assert chunks
    for ch in chunks:
        assert ch["char_count"] == len(ch["text"])
        assert ch["char_count"] <= 120 * 2  # small tails may be merged into the previous chunk
        assert ch["doc_id"] and ch["url"] and ch["section"].startswith(ch["title"])
        assert ch["embed_text"].startswith(ch["section"])  # contextual header


def test_chunk_ids_are_deterministic_and_unique(small_corpus):
    cfg = ChunkingConfig(strategy="section", max_chars=200)
    a = [c["chunk_id"] for c in chunk_corpus(small_corpus, cfg)]
    b = [c["chunk_id"] for c in chunk_corpus(small_corpus, cfg)]
    assert a == b and len(set(a)) == len(a)


def test_header_can_be_disabled(small_corpus):
    cfg = ChunkingConfig(strategy="section", max_chars=500, add_header=False)
    for ch in chunk_corpus(small_corpus, cfg):
        assert ch["embed_text"] == ch["text"]


def test_section_strategy_does_not_mix_sections(small_corpus):
    cfg = ChunkingConfig(strategy="section", max_chars=1000, min_chars=10)
    chunks = chunk_document(small_corpus[0], cfg)
    assert [c["section"] for c in chunks] == ["Wave transfers", "Wave transfers > Configuration"]


def test_overlap_repeats_text(small_corpus):
    cfg = ChunkingConfig(strategy="fixed_overlap", max_chars=100, overlap=40, min_chars=10)
    chunks = chunk_document(small_corpus[0], cfg)
    assert len(chunks) >= 2
    tail = chunks[0]["text"][-15:]
    assert tail.strip() and tail.strip()[:8] in chunks[1]["text"]


def test_split_long_text_never_exceeds_limit():
    text = " ".join(["word"] * 500)
    parts = split_long_text(text, 100)
    assert all(len(p) <= 100 for p in parts)
    assert " ".join(parts).split() == text.split()
