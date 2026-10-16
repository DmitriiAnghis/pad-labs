"""Grabber: incremental synchronisation of the source documents.

Algorithm (one run):
1. ``git`` sync (clone or fetch) -> list of files with blob SHA;
2. compare with the local manifest:
   * new path                -> download, status ``active``
   * same path, other SHA    -> re-download, ``updated_at`` changes
   * same path, same SHA     -> skipped (no work, no duplicates)
   * path disappeared        -> status ``deleted``, raw copy removed
3. identical content under two paths -> ``duplicate_of`` (preprocessing skips it);
4. each file is processed in its own try/except: one broken file never stops the run;
5. manifest is written atomically; a run report is appended to ``sync_log.jsonl``.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from common.config import PipelineConfig, Settings
from common.utils import read_json, sha256_text, utc_now, write_json
from grabber.git_source import GitSource

log = logging.getLogger("grabber")

MANIFEST_VERSION = 1


@dataclass
class SyncReport:
    started_at: str
    finished_at: str = ""
    commit: dict = field(default_factory=dict)
    total_remote: int = 0
    new: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    deleted: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"remote={self.total_remote} new={len(self.new)} updated={len(self.updated)} "
            f"unchanged={self.unchanged} deleted={len(self.deleted)} duplicates={len(self.duplicates)} "
            f"errors={len(self.errors)}"
        )


def doc_id_from_path(path: str, prefix: str) -> str:
    rel = path[len(prefix):] if path.startswith(prefix) else path
    return rel.rsplit(".", 1)[0]


def doc_url(path: str, base_url: str, branch: str) -> str:
    rel = path[len("content/"):] if path.startswith("content/") else path
    return f"{base_url.format(branch=branch)}/{rel.rsplit('.', 1)[0]}.html"


class RunLock:
    """Prevents two grabber runs from writing the same manifest at once."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise RuntimeError(
                f"another grabber run is in progress (lock file {self.path}). "
                "If no run is active, delete the lock file."
            )
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


class Grabber:
    def __init__(self, settings: Settings, config: PipelineConfig, source: GitSource | None = None) -> None:
        self.settings = settings
        self.config = config
        src = config.source
        self.source = source or GitSource(
            repo_url=settings.source_repo_url,
            branch=settings.source_branch,
            include=src.include,
            workdir=settings.sources_dir / src.name,
            extensions=src.extensions,
            timeout=src.git_timeout,
            retries=src.retries,
        )
        self.raw_dir = settings.raw_dir
        self.files_dir = self.raw_dir / "files"
        self.manifest_path = self.raw_dir / "manifest.json"

    # ------------------------------------------------------------------ manifest
    def load_manifest(self) -> dict:
        manifest = read_json(self.manifest_path, default=None)
        if not manifest:
            manifest = {"version": MANIFEST_VERSION, "source": {}, "last_sync": None, "documents": {}}
        return manifest

    def raw_path(self, doc_id: str) -> Path:
        return self.files_dir / f"{doc_id}.rst"

    # ------------------------------------------------------------------ run
    def run(self) -> SyncReport:
        with RunLock(self.raw_dir / ".lock"):
            return self._run()

    def _run(self) -> SyncReport:
        report = SyncReport(started_at=utc_now())
        manifest = self.load_manifest()
        docs: dict[str, dict] = manifest["documents"]
        src_cfg = self.config.source

        self.source.sync()
        commit = self.source.head_commit()
        report.commit = commit
        remote_files = self.source.list_files()
        report.total_remote = len(remote_files)
        log.info("source at commit %s (%s): %d files", commit["sha"][:10], commit["date"], len(remote_files))

        seen: set[str] = set()
        for rf in remote_files:
            doc_id = doc_id_from_path(rf.path, src_cfg.id_prefix)
            seen.add(doc_id)
            prev = docs.get(doc_id)
            if prev and prev.get("status") == "active" and prev.get("blob_sha") == rf.blob_sha and self.raw_path(doc_id).exists():
                report.unchanged += 1
                continue
            try:
                raw = self.source.read(rf.path)
                text = raw.decode("utf-8")
            except Exception as exc:  # noqa: BLE001 - a broken file must not stop the run
                log.error("failed to read %s: %s", rf.path, exc)
                report.errors.append({"doc_id": doc_id, "path": rf.path, "error": str(exc)})
                continue
            target = self.raw_path(doc_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            now = utc_now()
            record = {
                "doc_id": doc_id,
                "path": rf.path,
                "url": doc_url(rf.path, src_cfg.doc_base_url, self.settings.source_branch),
                "module": doc_id.split("/", 1)[0],
                "blob_sha": rf.blob_sha,
                "content_hash": sha256_text(text.replace("\r\n", "\n").strip()),
                "size": len(raw),
                "status": "active",
                "duplicate_of": None,
                "commit": commit["sha"],
                "first_seen": prev.get("first_seen", now) if prev else now,
                "updated_at": now,
                "deleted_at": None,
            }
            if prev and prev.get("status") == "active":
                report.updated.append(doc_id)
            else:
                report.new.append(doc_id)
            docs[doc_id] = record

        for doc_id, rec in docs.items():
            if doc_id not in seen and rec.get("status") == "active":
                rec["status"] = "deleted"
                rec["deleted_at"] = utc_now()
                self.raw_path(doc_id).unlink(missing_ok=True)
                report.deleted.append(doc_id)

        # duplicate detection by content hash among active documents
        by_hash: dict[str, str] = {}
        for doc_id in sorted(docs):
            rec = docs[doc_id]
            if rec.get("status") != "active":
                continue
            first = by_hash.setdefault(rec["content_hash"], doc_id)
            rec["duplicate_of"] = None if first == doc_id else first
            if first != doc_id:
                report.duplicates.append(doc_id)

        report.finished_at = utc_now()
        manifest["source"] = {
            "repo_url": self.settings.source_repo_url,
            "branch": self.settings.source_branch,
            "include": src_cfg.include,
        }
        manifest["last_sync"] = {"commit": commit, "finished_at": report.finished_at, "summary": report.summary()}
        write_json(self.manifest_path, manifest)
        with open(self.raw_dir / "sync_log.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(report.__dict__, ensure_ascii=False) + "\n")
        log.info("grabber finished: %s", report.summary())
        return report


def active_documents(manifest: dict) -> list[dict]:
    return [
        rec for rec in manifest.get("documents", {}).values()
        if rec.get("status") == "active" and not rec.get("duplicate_of")
    ]
