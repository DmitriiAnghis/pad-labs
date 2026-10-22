"""CLI commands (see run.py for usage)."""
from __future__ import annotations

import argparse
import json
import os
import logging
import sys

from common.config import Settings, load_pipeline_config, dotted_to_nested
from common.logging_setup import setup_logging

log = logging.getLogger("cli")


def _parse_set(values: list[str] | None) -> dict:
    """--set chunking.max_chars=500 --set reranker.backend=none"""
    flat = {}
    for item in values or []:
        if "=" not in item:
            raise SystemExit(f"--set expects key=value, got {item!r}")
        key, raw = item.split("=", 1)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        flat[key.strip()] = value
    return dotted_to_nested(flat)


def _load(args):
    settings = Settings.from_env()
    config = load_pipeline_config(getattr(args, "config", None), settings)
    config = config.with_overrides(_parse_set(getattr(args, "set", None)))
    return settings, config


def cmd_grab(args) -> int:
    from grabber.grabber import Grabber

    settings, config = _load(args)
    report = Grabber(settings, config).run(force=args.force)
    print(report.summary())
    for label, items in (("new", report.new), ("updated", report.updated), ("deleted", report.deleted)):
        if items and len(items) <= 20:
            for doc_id in items:
                print(f"  {label}: {doc_id}")
    for err in report.errors:
        print(f"  error: {err['doc_id']}: {err['error']}")
    return 1 if report.errors and not (report.new or report.updated or report.unchanged) else 0


def cmd_process(args) -> int:
    from preprocessing.documents import process_corpus

    settings, config = _load(args)
    report = process_corpus(settings, config, force=args.force)
    print(report.summary())
    return 0


def cmd_chunk(args) -> int:
    from preprocessing.chunking import chunk_corpus, chunk_stats
    from preprocessing.documents import load_documents

    settings, config = _load(args)
    docs = load_documents(settings)
    chunks = chunk_corpus(docs, config.chunking)
    print(json.dumps(chunk_stats(chunks), indent=2))
    if args.show:
        for ch in chunks[: args.show]:
            print("-" * 80)
            print(ch["chunk_id"], "|", ch["section"], "|", ch["url"])
            print(ch["text"])
    return 0


def cmd_index(args) -> int:
    from retrieval.index import build_index

    settings, config = _load(args)
    index = build_index(settings, config, force=args.force)
    print(f"index {index.key}: {len(index.chunks)} chunks, dense={'yes' if index.dense is not None else 'no'}")
    print(f"saved to {index.path}")
    return 0


def _print_candidates(cands, max_chars=220):
    for c in cands:
        ch = c.chunk
        print(f"#{c.rank:<2} score={c.score:.4f} [{c.source}] {ch['doc_id']} :: {ch['section']}")
        print("     " + ch["text"][:max_chars].replace("\n", " ") + ("..." if len(ch["text"]) > max_chars else ""))


def cmd_search(args) -> int:
    from pipeline.rag import RagPipeline

    settings, config = _load(args)
    rag = RagPipeline.from_config(settings, config)
    result = rag.retrieve(args.query)
    print(f"query: {result.query!r} (lang={result.language}, search query={result.search_query!r})")
    print(f"retrieved {len(result.retrieved)} | after filters {len(result.filtered)} | after reranker {len(result.reranked)}")
    print(json.dumps(result.timings_ms, indent=None))
    print("== final context candidates")
    _print_candidates(result.reranked)
    if args.verbose:
        print("== retriever top-k")
        _print_candidates(result.retrieved)
        for d in result.dropped:
            print("dropped:", d)
    return 0


def cmd_ask(args) -> int:
    from pipeline.rag import RagPipeline

    settings, config = _load(args)
    rag = RagPipeline.from_config(settings, config)
    answer = rag.answer(args.query)
    print(answer.text)
    print()
    if answer.sources:
        print("Источники / Sources:")
        for s in answer.sources:
            print(f"  [{s['n']}] {s['title']} - {s['section']} - {s['url']}")
    print()
    print(f"refused={answer.refused} reason={answer.refusal_reason} model={answer.model} timings_ms={json.dumps(answer.timings_ms)}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    # uvicorn imports the app itself, so --config / --set reach it through the environment
    if getattr(args, "config", None):
        os.environ["RAG_CONFIG"] = args.config
    overrides = _parse_set(getattr(args, "set", None))
    if overrides:
        os.environ["RAG_OVERRIDES"] = json.dumps(overrides)
    settings = Settings.from_env()
    uvicorn.run("api.app:app", host=args.host or settings.api_host, port=args.port or settings.api_port, log_config=None, access_log=False)
    return 0


def cmd_eval(args) -> int:
    settings, config = _load(args)
    if args.what == "retrieval":
        from evaluation.retrieval_eval import run_retrieval_eval

        result = run_retrieval_eval(settings, config, lang=args.lang, ks=(1, 3, 5, 10, 20))
        print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
    else:
        from evaluation.generation_eval import run_generation_eval

        result = run_generation_eval(settings, config, lang=args.lang, judge=args.judge, limit=args.limit)
        print(json.dumps(result["summary"], indent=2, ensure_ascii=False))
        print(f"details: {result.get('path')}")
    return 0


def cmd_experiment(args) -> int:
    from evaluation.experiments import list_experiments, run_experiment

    settings, config = _load(args)
    if args.name == "list":
        for name, desc in list_experiments():
            print(f"{name:<22} {desc}")
        return 0
    names = [n for n, _ in list_experiments()] if args.name == "all" else [n.strip() for n in args.name.split(",")]
    only = [v.strip() for v in args.only.split(",")] if args.only else None
    for name in names:
        run_experiment(settings, name, lang=args.lang, limit=args.limit or None, only=only)
    return 0


def cmd_report(args) -> int:
    from evaluation.report import build_report

    settings = Settings.from_env()
    path = build_report(settings)
    print(f"report written to {path}")
    return 0


def cmd_models(args) -> int:
    from embeddings.minilm import download_minilm

    settings = Settings.from_env()
    if args.action == "download" and args.name == "minilm":
        path = download_minilm(settings.models_dir)
        print(f"MiniLM ONNX ready at {path}")
        return 0
    raise SystemExit("usage: python run.py models download minilm")


def cmd_status(args) -> int:
    from common.utils import read_json

    settings, config = _load(args)
    manifest = read_json(settings.raw_dir / "manifest.json", default=None)
    print("== settings")
    for key in ("data_dir", "ollama_url", "llm_model", "llm_model_alt", "judge_model", "embed_model", "rerank_model", "source_branch"):
        print(f"  {key}: {getattr(settings, key)}")
    print("== pipeline config")
    print(f"  index key: {config.index_key()}")
    print(f"  retrieval={config.retrieval.mode} top_k={config.retrieval.top_k} reranker={config.reranker.backend} top_n={config.reranker.top_n} generation={config.generation.backend}")
    print("== data")
    if manifest:
        docs = manifest["documents"].values()
        active = sum(1 for d in docs if d["status"] == "active")
        print(f"  manifest: {active} active / {len(manifest['documents'])} known; last sync: {manifest['last_sync']}")
    else:
        print("  manifest: none (run `python run.py grab`)")
    state = read_json(settings.processed_dir / "state.json", default=None)
    print(f"  processed: {state['summary'] if state else 'none'}")
    if settings.indexes_dir.exists():
        for p in sorted(settings.indexes_dir.iterdir()):
            meta = read_json(p / "meta.json", default={}) or {}
            print(f"  index: {p.name} ({meta.get('chunks', '?')} chunks, built {meta.get('built_at', '?')})")
    from generation.ollama_client import OllamaClient

    client = OllamaClient(settings)
    ok, info = client.health()
    print(f"== ollama {settings.ollama_url}: {'OK' if ok else 'unavailable'} {info}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run.py", description="Lab 1: RAG over Odoo Inventory documentation")
    p.add_argument("--config", help="pipeline YAML (default: RAG_CONFIG or configs/default.yaml)")
    p.add_argument("--set", action="append", help="override a config value, e.g. --set chunking.max_chars=500")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("grab", help="download / update documents (incremental)")
    sp.add_argument("--force", action="store_true", help="re-save every file even if its blob SHA is unchanged")
    sp.set_defaults(func=cmd_grab)
    sp = sub.add_parser("process", help="RST -> clean structured documents")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_process)
    sp = sub.add_parser("chunk", help="show chunking statistics for the current config")
    sp.add_argument("--show", type=int, default=0, help="print first N chunks")
    sp.set_defaults(func=cmd_chunk)
    sp = sub.add_parser("index", help="build chunks + embeddings + vector store + BM25")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_index)
    sp = sub.add_parser("search", help="retrieval only")
    sp.add_argument("query")
    sp.set_defaults(func=cmd_search)
    sp = sub.add_parser("ask", help="full RAG answer")
    sp.add_argument("query")
    sp.set_defaults(func=cmd_ask)
    sp = sub.add_parser("serve", help="REST API + web UI")
    sp.add_argument("--host")
    sp.add_argument("--port", type=int)
    sp.set_defaults(func=cmd_serve)
    sp = sub.add_parser("eval", help="evaluate on data/eval/questions.jsonl")
    sp.add_argument("what", choices=["retrieval", "generation"])
    sp.add_argument("--lang", choices=["en", "ru"], default="en")
    sp.add_argument("--judge", action="store_true", help="LLM-as-a-judge scores (Ollama, JUDGE_MODEL)")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_eval)
    sp = sub.add_parser("experiment", help="run experiment suites from configs/experiments.yaml ('list', 'all' or names separated by commas)")
    sp.add_argument("name")
    sp.add_argument("--lang", choices=["en", "ru"], default=None)
    sp.add_argument("--only", help="run only these variants (names separated by commas)")
    sp.add_argument("--limit", type=int, default=0)
    sp.set_defaults(func=cmd_experiment)
    sub.add_parser("report", help="rebuild experiments/REPORT.md from saved results").set_defaults(func=cmd_report)
    sp = sub.add_parser("models", help="download local models")
    sp.add_argument("action", choices=["download"])
    sp.add_argument("name", choices=["minilm"])
    sp.set_defaults(func=cmd_models)
    sub.add_parser("status", help="show configuration and data state").set_defaults(func=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.from_env()
    setup_logging("DEBUG" if args.verbose else settings.log_level, settings.log_format)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI prints a readable error instead of a stack trace
        if args.verbose:
            raise
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
