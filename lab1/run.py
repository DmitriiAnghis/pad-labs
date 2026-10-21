"""Command line entry point of the lab 1 RAG system.

    python run.py grab                 # download / update the documentation (incremental)
    python run.py process              # RST -> clean structured documents
    python run.py index                # chunks -> embeddings -> vector store + BM25
    python run.py search "question"    # retrieval only (debug)
    python run.py ask "question"       # full RAG answer with sources
    python run.py serve                # REST API + small web UI
    python run.py eval retrieval       # retrieval metrics on the test set
    python run.py eval generation      # answer quality on the test set (needs Ollama)
    python run.py eval generation --questions attacks.jsonl   # prompt-injection questions
    python run.py rate data/runs/generation_en_....json        # rate the answers by hand (1-5 + flags)
    python run.py ratings --save       # summary of manual ratings per model/prompt
    python run.py experiment chunking  # one experiment suite from configs/experiments.yaml
    python run.py models download minilm
    python run.py status               # what is downloaded / indexed / configured
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
