import sys
from pathlib import Path

import pytest

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "src"))

from common.config import PipelineConfig, Settings  # noqa: E402


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Settings with an empty temporary data directory (tests never touch data/ and never send traces)."""
    return Settings.from_env().model_copy(update={"data_dir": tmp_path / "data", "ollama_url": "http://127.0.0.1:9",
                                                  "langfuse_public_key": None, "langfuse_secret_key": None})


@pytest.fixture
def config() -> PipelineConfig:
    return PipelineConfig()


def make_doc(doc_id: str, title: str, sections: list[tuple[list[str], list[str]]], module: str = "inventory") -> dict:
    """Processed document in the format of preprocessing/documents.py."""
    secs = []
    for path, paragraphs in sections:
        secs.append({
            "heading_path": path,
            "title": path[-1],
            "level": len(path),
            "anchor": path[-1].lower().replace(" ", "-"),
            "url": f"https://example.test/{doc_id}.html",
            "blocks": [{"type": "paragraph", "text": p} for p in paragraphs],
        })
    return {
        "doc_id": doc_id, "title": title, "url": f"https://example.test/{doc_id}.html", "path": doc_id + ".rst",
        "module": module, "category": doc_id.split("/")[1] if "/" in doc_id else "", "blob_sha": "x",
        "updated_at": "2026-01-01T00:00:00Z", "processor_version": 1, "sections": secs,
        "char_count": sum(len(p) for _, ps in sections for p in ps),
    }


@pytest.fixture
def small_corpus() -> list[dict]:
    return [
        make_doc("inventory/wave", "Wave transfers", [
            (["Wave transfers"], ["A wave transfer contains parts of different pickings, split before being grouped in a batch."]),
            (["Wave transfers", "Configuration"], ["To enable wave picking, go to Inventory > Configuration > Settings and tick the Batch, Wave & Cluster Transfers checkbox. The Storage Locations and Multi-Step Routes options must also be enabled."]),
        ]),
        make_doc("inventory/fefo", "FEFO removal", [
            (["FEFO removal"], ["The First Expired, First Out (FEFO) removal strategy picks products based on their removal dates, so lots that expire first are delivered first."]),
            (["FEFO removal", "Removal date"], ["The removal date is the expiration date minus the number of days set in the Removal Date field of the product form."]),
        ]),
        make_doc("barcode/rfid", "RFID", [
            (["RFID"], ["Only ultra-high frequency (UHF) RFID tags are supported. SGTIN-96 is the only supported encoding scheme for RFID tags in Odoo."]),
        ], module="barcode"),
        make_doc("inventory/scrap", "Scrap inventory", [
            (["Scrap inventory"], ["Damaged products that cannot be repaired or returned are scrapped and moved to the virtual location Virtual Locations/Scrap."]),
        ]),
    ]
