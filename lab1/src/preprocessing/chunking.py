"""Chunking strategies (the experiment compares all four on the same corpus and test set).

* ``fixed``         - windows of ``max_chars`` over the plain document text (cut snapped to a space,
                      so words are not split). Ignores structure: the simplest baseline.
* ``fixed_overlap`` - the same with ``overlap`` characters repeated between neighbouring windows,
                      so a fact on a boundary is complete in at least one chunk.
* ``paragraph``     - whole blocks (paragraph / list / note / table) packed greedily up to
                      ``max_chars``; may cross section boundaries; an oversized block is split by
                      sentences.
* ``section``       - one chunk per document section (heading); a long section is split by
                      blocks, a tiny one is merged with its neighbour.

All strategies return chunks with the same metadata (doc, section path, URL with anchor), so
sources can be shown for any of them. ``embed_text`` optionally prepends "Doc title > Section"
(contextual header) to the text that is embedded - a short chunk keeps the topic it belongs to.
"""
from __future__ import annotations

import re
import statistics
from bisect import bisect_right

from common.config import ChunkingConfig
from common.utils import short_hash

SENTENCE_SPLIT = re.compile(r"(?<=[.!?:])\s+(?=[A-Z0-9\"'(\[])|\n+")


def split_long_text(text: str, max_chars: int) -> list[str]:
    """Split text by sentences (then by words) into pieces of at most max_chars."""
    if len(text) <= max_chars:
        return [text]
    pieces: list[str] = []
    buf = ""
    for sent in SENTENCE_SPLIT.split(text):
        sent = sent.strip()
        if not sent:
            continue
        while len(sent) > max_chars:  # a single monster sentence: cut by words
            cut = sent.rfind(" ", 0, max_chars)
            cut = cut if cut > max_chars // 2 else max_chars
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        candidate = f"{buf} {sent}".strip() if buf else sent
        if len(candidate) > max_chars and buf:
            pieces.append(buf)
            buf = sent
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces


def _header(doc: dict, heading_path: list[str]) -> str:
    path = heading_path[1:] if heading_path and heading_path[0] == doc["title"] else heading_path
    return " > ".join([doc["title"], *path]) if path else doc["title"]


def _make_chunk(doc: dict, sec: dict, text: str, position: int, cfg: ChunkingConfig) -> dict:
    header = _header(doc, sec["heading_path"])
    key = cfg.key()
    return {
        "chunk_id": short_hash(f"{key}|{doc['doc_id']}|{position}", 16),
        "doc_id": doc["doc_id"],
        "title": doc["title"],
        "section": header,
        "heading_path": sec["heading_path"],
        "url": sec.get("url", doc["url"]),
        "module": doc.get("module", ""),
        "category": doc.get("category", ""),
        "updated_at": doc.get("updated_at", ""),
        "position": position,
        "strategy": cfg.strategy,
        "config_key": key,
        "text": text,
        "embed_text": f"{header}\n{text}" if cfg.add_header else text,
        "char_count": len(text),
    }


# ---------------------------------------------------------------- fixed / fixed_overlap
def _flatten(doc: dict) -> tuple[str, list[int], list[dict]]:
    """Plain text of the doc + start offset of each section (to map a chunk back to its section)."""
    text_parts: list[str] = []
    offsets: list[int] = []
    sections: list[dict] = []
    pos = 0
    for sec in doc["sections"]:
        offsets.append(pos)
        sections.append(sec)
        lines = []
        if len(sec["heading_path"]) > 1:
            lines.append(sec["heading_path"][-1])
        lines.extend(b["text"] for b in sec["blocks"])
        part = "\n".join(lines) + "\n\n"
        text_parts.append(part)
        pos += len(part)
    return "".join(text_parts).strip(), offsets, sections


def chunk_fixed(doc: dict, cfg: ChunkingConfig, overlap: int) -> list[dict]:
    text, offsets, sections = _flatten(doc)
    size = cfg.max_chars
    overlap = max(0, min(overlap, size // 2))
    chunks: list[dict] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:  # snap to the last whitespace so a word is not cut in half
            cut = text.rfind(" ", start + size // 2, end)
            nl = text.rfind("\n", start + size // 2, end)
            cut = max(cut, nl)
            if cut > start:
                end = cut
        piece = text[start:end].strip()
        if piece:
            sec = sections[max(0, bisect_right(offsets, start) - 1)]
            chunks.append(_make_chunk(doc, sec, piece, len(chunks), cfg))
        if end >= n:
            break
        next_start = end - overlap if overlap else end
        if overlap:  # start the overlap at a word boundary too
            sp = text.find(" ", next_start, end)
            next_start = sp + 1 if sp != -1 else next_start
        start = max(next_start, start + 1)
        while start < n and text[start].isspace():
            start += 1
    return chunks


# ---------------------------------------------------------------- paragraph
def _units(doc: dict, max_chars: int) -> list[tuple[int, str]]:
    """(section index, text) for every block; oversized blocks are split by sentences."""
    units = []
    for si, sec in enumerate(doc["sections"]):
        for b in sec["blocks"]:
            for piece in split_long_text(b["text"], max_chars):
                units.append((si, piece))
    return units


def chunk_paragraph(doc: dict, cfg: ChunkingConfig) -> list[dict]:
    sections = doc["sections"]
    chunks: list[dict] = []
    buf: list[str] = []
    buf_sec: int | None = None
    last_sec: int | None = None
    for si, text in _units(doc, cfg.max_chars):
        piece = text
        if last_sec is not None and si != last_sec and len(sections[si]["heading_path"]) > 1:
            piece = sections[si]["heading_path"][-1] + "\n" + text  # keep the heading when crossing sections
        current = "\n".join(buf)
        if buf and len(current) + 1 + len(piece) > cfg.max_chars:
            chunks.append(_make_chunk(doc, sections[buf_sec], current, len(chunks), cfg))
            buf, buf_sec = [], None
            piece = text if not (len(sections[si]["heading_path"]) > 1 and si != last_sec) else piece
        if not buf:
            buf_sec = si
            piece = text  # the header of the chunk already names the section
        buf.append(piece)
        last_sec = si
    if buf:
        chunks.append(_make_chunk(doc, sections[buf_sec], "\n".join(buf), len(chunks), cfg))
    return chunks


# ---------------------------------------------------------------- section
def chunk_section(doc: dict, cfg: ChunkingConfig) -> list[dict]:
    chunks: list[dict] = []
    for sec in doc["sections"]:
        blocks = []
        for b in sec["blocks"]:
            blocks.extend(split_long_text(b["text"], cfg.max_chars))
        buf: list[str] = []
        for text in blocks:
            if buf and len("\n".join(buf)) + 1 + len(text) > cfg.max_chars:
                chunks.append(_make_chunk(doc, sec, "\n".join(buf), len(chunks), cfg))
                buf = []
            buf.append(text)
        if buf:
            chunks.append(_make_chunk(doc, sec, "\n".join(buf), len(chunks), cfg))
    return chunks


# ---------------------------------------------------------------- common
def _merge_small(chunks: list[dict], cfg: ChunkingConfig) -> list[dict]:
    """Chunks shorter than min_chars are glued to the next chunk of the same document
    (or to the previous one if they are last), as long as the result stays <= 1.5 * max_chars."""
    if cfg.min_chars <= 0 or len(chunks) < 2:
        return chunks
    out: list[dict] = []
    carry: dict | None = None
    for ch in chunks:
        if carry is not None:
            merged_text = carry["text"] + "\n" + ch["text"]
            if len(merged_text) <= cfg.max_chars * 1.5:
                ch = dict(ch)
                # keep the earlier section as the chunk's anchor, it starts there
                for key in ("section", "heading_path", "url"):
                    ch[key] = carry[key]
                ch["text"] = merged_text
                ch["embed_text"] = (f"{ch['section']}\n{merged_text}" if cfg.add_header else merged_text)
                ch["char_count"] = len(merged_text)
            else:
                out.append(carry)
            carry = None
        if ch["char_count"] < cfg.min_chars:
            carry = ch
            continue
        out.append(ch)
    if carry is not None:
        if out:
            prev = dict(out[-1])
            prev["text"] = prev["text"] + "\n" + carry["text"]
            prev["embed_text"] = (f"{prev['section']}\n{prev['text']}" if cfg.add_header else prev["text"])
            prev["char_count"] = len(prev["text"])
            out[-1] = prev
        else:
            out.append(carry)
    for i, ch in enumerate(out):
        ch["position"] = i
        ch["chunk_id"] = short_hash(f"{cfg.key()}|{ch['doc_id']}|{i}", 16)
    return out


def chunk_document(doc: dict, cfg: ChunkingConfig) -> list[dict]:
    if cfg.strategy == "fixed":
        chunks = chunk_fixed(doc, cfg, overlap=0)
    elif cfg.strategy == "fixed_overlap":
        chunks = chunk_fixed(doc, cfg, overlap=cfg.overlap)
    elif cfg.strategy == "paragraph":
        chunks = chunk_paragraph(doc, cfg)
    elif cfg.strategy == "section":
        chunks = chunk_section(doc, cfg)
    else:  # pragma: no cover - validated by pydantic
        raise ValueError(f"unknown chunking strategy {cfg.strategy}")
    return _merge_small(chunks, cfg)


def chunk_corpus(docs: list[dict], cfg: ChunkingConfig) -> list[dict]:
    chunks: list[dict] = []
    for doc in docs:
        chunks.extend(chunk_document(doc, cfg))
    return chunks


def chunk_stats(chunks: list[dict]) -> dict:
    sizes = [c["char_count"] for c in chunks] or [0]
    sizes_sorted = sorted(sizes)
    return {
        "chunks": len(chunks),
        "documents": len({c["doc_id"] for c in chunks}),
        "chars_mean": round(statistics.mean(sizes), 1),
        "chars_median": statistics.median(sizes),
        "chars_p90": sizes_sorted[int(len(sizes_sorted) * 0.9)] if len(sizes_sorted) > 1 else sizes_sorted[0],
        "chars_min": sizes_sorted[0],
        "chars_max": sizes_sorted[-1],
        "total_chars": sum(sizes),
    }
