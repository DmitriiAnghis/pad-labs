"""Preprocessing stage: raw RST files -> clean structured documents (data/processed/docs.jsonl).

Two passes over the corpus:
1. collect document titles and RST labels of all files (``:ref:`` / ``:doc:`` links between
   pages are resolved to real titles, e.g. ``:ref:`inventory/.../auto-waves``` -> "Automatic waves");
2. parse every document with these maps.

Incrementality: the stage stores a fingerprint of its input (blob SHAs of active documents
+ processor version + options). If nothing changed, the run is skipped. If something changed,
the whole corpus is re-parsed: it takes ~1 s and links are global (a renamed section in one
file changes the text of another file), so per-file reprocessing would give stale text.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from common.config import PipelineConfig, Settings
from common.utils import read_json, read_jsonl, sha256_text, utc_now, write_json, write_jsonl
from grabber.grabber import active_documents
from preprocessing.rst_parser import RstParser

log = logging.getLogger("preprocessing")


@dataclass
class ProcessReport:
    skipped: bool
    documents: int = 0
    dropped_small: int = 0
    sections: int = 0
    blocks: int = 0
    chars: int = 0
    changed: list[str] | None = None

    def summary(self) -> str:
        if self.skipped:
            return "no changes since last run - skipped"
        return (
            f"documents={self.documents} dropped_small={self.dropped_small} sections={self.sections} "
            f"blocks={self.blocks} chars={self.chars} changed={len(self.changed or [])}"
        )


def document_text(doc: dict) -> str:
    """Plain text of a processed document (used by fixed-size chunking and statistics)."""
    parts = []
    for sec in doc["sections"]:
        if sec["heading_path"] and len(sec["heading_path"]) > 1:
            parts.append(sec["heading_path"][-1])
        parts.extend(b["text"] for b in sec["blocks"])
    return "\n\n".join(parts)


def process_corpus(settings: Settings, config: PipelineConfig, force: bool = False) -> ProcessReport:
    manifest = read_json(settings.raw_dir / "manifest.json", default=None)
    if not manifest:
        raise RuntimeError("manifest not found - run `python run.py grab` first")
    docs_meta = sorted(active_documents(manifest), key=lambda d: d["doc_id"])
    pcfg = config.processing
    fingerprint = sha256_text(
        "|".join(f"{d['doc_id']}:{d['blob_sha']}" for d in docs_meta)
        + f"|v{pcfg.version}|alt{pcfg.keep_image_alt}|see{pcfg.keep_seealso}|min{pcfg.min_doc_chars}"
    )
    state_path = settings.processed_dir / "state.json"
    out_path = settings.processed_dir / "docs.jsonl"
    state = read_json(state_path, default={}) or {}
    if not force and state.get("fingerprint") == fingerprint and out_path.exists():
        log.info("preprocessing: input unchanged (fingerprint %s) - skipped", fingerprint[:12])
        return ProcessReport(skipped=True)

    texts = {}
    for meta in docs_meta:
        path = settings.raw_dir / "files" / f"{meta['doc_id']}.rst"
        try:
            texts[meta["doc_id"]] = path.read_text(encoding="utf-8")
        except OSError as exc:
            log.error("cannot read raw file %s: %s", path, exc)

    # pass 1: titles and labels
    doc_titles: dict[str, str] = {}
    label_titles: dict[str, str] = {}
    for doc_id, text in texts.items():
        parsed = RstParser(doc_id=doc_id).parse(text)
        doc_titles[doc_id] = parsed.title
        label_titles.update(parsed.labels)

    # pass 2: full parse with resolved links
    previous = {d["doc_id"]: d.get("blob_sha") for d in read_jsonl(out_path)} if out_path.exists() else {}
    report = ProcessReport(skipped=False, changed=[])
    rows = []
    for meta in docs_meta:
        doc_id = meta["doc_id"]
        if doc_id not in texts:
            continue
        parser = RstParser(
            label_titles=label_titles,
            doc_titles=doc_titles,
            doc_id=doc_id,
            keep_image_alt=pcfg.keep_image_alt,
            keep_seealso=pcfg.keep_seealso,
        )
        try:
            parsed = parser.parse(texts[doc_id])
        except Exception as exc:  # noqa: BLE001 - one broken document must not stop the stage
            log.exception("failed to parse %s: %s", doc_id, exc)
            continue
        url = meta["url"]
        sections = []
        for sec in parsed.sections:
            sec = dict(sec)
            sec["url"] = url + (f"#{sec['anchor']}" if sec["anchor"] else "")
            sections.append(sec)
        doc = {
            "doc_id": doc_id,
            "title": parsed.title or doc_id.rsplit("/", 1)[-1],
            "url": url,
            "path": meta["path"],
            "module": meta["module"],
            "category": doc_id.split("/")[1] if doc_id.count("/") >= 2 else "",
            "blob_sha": meta["blob_sha"],
            "updated_at": meta["updated_at"],
            "processor_version": pcfg.version,
            "sections": sections,
        }
        text = document_text(doc)
        if len(text) < pcfg.min_doc_chars:
            report.dropped_small += 1
            continue
        doc["char_count"] = len(text)
        rows.append(doc)
        if previous.get(doc_id) != meta["blob_sha"]:
            report.changed.append(doc_id)
        report.sections += len(sections)
        report.blocks += sum(len(s["blocks"]) for s in sections)
        report.chars += len(text)
    report.documents = len(rows)
    write_jsonl(out_path, rows)
    write_json(state_path, {"fingerprint": fingerprint, "processed_at": utc_now(), "summary": report.summary()})
    log.info("preprocessing finished: %s", report.summary())
    return report


def load_documents(settings: Settings) -> list[dict]:
    path = settings.processed_dir / "docs.jsonl"
    docs = read_jsonl(path)
    if not docs:
        raise RuntimeError(f"{path} is empty - run `python run.py grab` and `python run.py process` first")
    return docs
