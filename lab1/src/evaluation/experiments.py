"""Experiment runner: variants from configs/experiments.yaml -> experiments/results/<name>.json.

For every variant the default pipeline config is overridden with the variant's settings, the
index is built (or loaded from data/indexes), and the same questions are evaluated. Summaries
go to experiments/results (committed, used by the report); per-question details go to
data/runs/experiments (not committed, for debugging single questions).
"""
from __future__ import annotations

import importlib.util
import itertools
import logging
import platform
import re
import time
from pathlib import Path

import yaml

from common.config import LAB_ROOT, Settings, dotted_to_nested, load_pipeline_config
from common.utils import read_json, utc_now, write_json
from evaluation.dataset import load_questions

log = logging.getLogger("evaluation.experiments")

EXPERIMENTS_FILE = LAB_ROOT / "configs" / "experiments.yaml"
RESULTS_DIR = LAB_ROOT / "experiments" / "results"

_VAR = re.compile(r"\$\{(\w+)\}")


def load_experiments(path: Path = EXPERIMENTS_FILE) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def list_experiments() -> list[tuple[str, str]]:
    return [(name, exp.get("description", "")) for name, exp in load_experiments().items()]


def _substitute(value, settings: Settings):
    if isinstance(value, str):
        return _VAR.sub(lambda m: str(getattr(settings, m.group(1).lower(), m.group(0))), value)
    return value


def _fmt(v) -> str:
    return "null" if v is None else str(v).lower() if isinstance(v, bool) else str(v)


def expand_variants(exp: dict) -> list[dict]:
    """grid (cartesian product) + explicit variants -> [{name, set, requires}]"""
    out: list[dict] = []
    grid = exp.get("grid") or {}
    if grid:
        keys = list(grid)
        for values in itertools.product(*(grid[k] for k in keys)):
            name = ",".join(f"{k.split('.')[-1]}={_fmt(v)}" for k, v in zip(keys, values))
            out.append({"name": name, "set": dict(zip(keys, values)), "requires": list(exp.get("requires", []))})
    for v in exp.get("variants") or []:
        out.append({"name": v["name"], "set": v.get("set") or {}, "requires": list(exp.get("requires", [])) + list(v.get("requires", []))})
    return out


class Resources:
    """Checks optional resources once per run."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._cache: dict[str, tuple[bool, str]] = {}

    def check(self, name: str) -> tuple[bool, str]:
        if name not in self._cache:
            if name == "ollama":
                from generation.ollama_client import OllamaClient

                ok, info = OllamaClient(self.settings).health()
                self._cache[name] = (ok, info if ok else f"Ollama not reachable at {self.settings.ollama_url}")
            elif name == "sentence_transformers":
                ok = importlib.util.find_spec("sentence_transformers") is not None
                self._cache[name] = (ok, "" if ok else "sentence-transformers not installed (pip install -r requirements-extra.txt)")
            elif name == "qdrant":
                ok = importlib.util.find_spec("qdrant_client") is not None
                self._cache[name] = (ok, "" if ok else "qdrant-client not installed (pip install -r requirements-extra.txt)")
            else:
                self._cache[name] = (False, f"unknown resource {name}")
        return self._cache[name]

    def missing(self, names: list[str]) -> str | None:
        reasons = [self.check(n)[1] for n in names if not self.check(n)[0]]
        return "; ".join(reasons) if reasons else None


def _merge_rows(old: list[dict], new: list[dict]) -> list[dict]:
    """Rows keyed by (variant, lang); a skipped/failed new row never replaces a real old result."""
    key = lambda r: (r["variant"], r["lang"])  # noqa: E731
    merged = {key(r): r for r in old}
    for r in new:
        prev = merged.get(key(r))
        if prev and "summary" in prev and "summary" not in r:
            continue
        merged[key(r)] = r
    order = [key(r) for r in new] + [k for k in merged if k not in {key(r) for r in new}]
    return [merged[k] for k in order]


def run_experiment(settings: Settings, name: str, lang: str | None = None, limit: int | None = None, only: list[str] | None = None) -> dict:
    from evaluation.generation_eval import Judge, evaluate_generation
    from evaluation.retrieval_eval import evaluate_retrieval
    from pipeline.rag import RagPipeline

    experiments = load_experiments()
    if name not in experiments:
        raise SystemExit(f"unknown experiment '{name}'. Available: {', '.join(experiments)}")
    exp = experiments[name]
    kind = exp.get("kind", "retrieval")
    langs = [lang] if lang else exp.get("langs", ["en"])
    base = load_pipeline_config(settings.config_path, settings)
    base = base.with_overrides(dotted_to_nested({k: _substitute(v, settings) for k, v in (exp.get("base") or {}).items()}))
    questions = load_questions(settings.eval_dir / "questions.jsonl", limit=limit)
    resources = Resources(settings)
    variants = [v for v in expand_variants(exp) if not only or v["name"] in only]

    print(f"\n=== experiment '{name}': {exp.get('description', '')}")
    print(f"    {len(variants)} variants x {len(langs)} language(s), {len(questions)} questions, kind={kind}")
    rows: list[dict] = []
    for variant in variants:
        overrides = {k: _substitute(v, settings) for k, v in variant["set"].items()}
        missing = resources.missing(variant["requires"])
        for lg in langs:
            row = {"variant": variant["name"], "lang": lg, "overrides": overrides}
            if missing:
                row["skipped"] = missing
                print(f"  - {variant['name']:<34} {lg}  skipped: {missing}")
                rows.append(row)
                continue
            try:
                config = base.with_overrides(dotted_to_nested(overrides))
                t0 = time.perf_counter()
                pipeline = RagPipeline.from_config(settings, config)
                build_s = time.perf_counter() - t0
                if kind == "retrieval":
                    result = evaluate_retrieval(pipeline, questions, lg)
                else:
                    judge = None
                    if exp.get("judge") and resources.check("ollama")[0] and config.generation.backend == "ollama":
                        judge = Judge(settings)
                    result = evaluate_generation(pipeline, questions, lg, judge)
                meta = pipeline.index.meta
                row["index"] = {
                    "key": meta.get("key"),
                    "chunks": meta.get("chunks"),
                    "chars_mean": (meta.get("chunk_stats") or {}).get("chars_mean"),
                    "embedder": meta.get("embedder"),
                    "embed_seconds": meta.get("embed_seconds"),
                    "truncated_inputs": meta.get("truncated_inputs"),
                    "load_or_build_seconds": round(build_s, 2),
                }
                row["config_fingerprint"] = config.fingerprint()
                row["summary"] = result["summary"]
                write_json(settings.runs_dir / "experiments" / name / f"{_safe(variant['name'])}_{lg}.json", {"config": config.model_dump(), **result})
                print(f"  - {variant['name']:<34} {lg}  {_headline(kind, result['summary'])}")
            except Exception as exc:  # one broken variant (e.g. model not pulled) must not stop the others
                log.exception("variant %s failed", variant["name"])
                row["error"] = f"{type(exc).__name__}: {exc}"
                print(f"  - {variant['name']:<34} {lg}  ERROR: {row['error']}")
            rows.append(row)

    path = RESULTS_DIR / f"{name}.json"
    old = read_json(path, default=None) or {}
    data = {
        "experiment": name,
        "description": exp.get("description", ""),
        "kind": kind,
        "focus": exp.get("focus"),
        "updated_at": utc_now(),
        "questions": len(questions),
        "machine": {"platform": platform.platform(), "python": platform.python_version(), "processor": platform.processor() or platform.machine()},
        "base_config": base.model_dump(),
        "rows": _sorted(_merge_rows(old.get("rows", []), rows), [v["name"] for v in expand_variants(exp)], langs),
    }
    write_json(path, data)
    print(f"    results: {path}")
    return data


def _sorted(rows: list[dict], variant_order: list[str], langs: list[str]) -> list[dict]:
    """Rows in the order of the YAML file (variant, then language)."""
    pos = {n: i for i, n in enumerate(variant_order)}
    return sorted(rows, key=lambda r: (pos.get(r["variant"], len(pos)), r["lang"] not in langs, r["lang"]))


def _safe(name: str) -> str:
    return re.sub(r"[^\w.=,-]+", "_", name)


def _headline(kind: str, s: dict) -> str:
    if kind == "retrieval":
        f = s["final"]
        return (f"MRR={f['mrr']:.3f} Hit@1={f['hit@1']:.3f} Hit@5={f['hit@5']:.3f} R@5={f['recall@5']:.3f} "
                f"Ev@5={f['evidence@5']:.3f} Ev@ctx={f.get('evidence@ctx', 0):.3f} R@20(ret)={s['retriever']['recall@20']:.3f} p50={s['latency_ms']['p50']}ms")
    return (f"behaviour={s['behaviour_accuracy']} no_answer_refused={s['no_answer_refused']} false_refusals={s['false_refusals']} "
            f"keywords={s['keyword_coverage']} citations={s['citation_rate']} p50={s['latency_ms']['p50']}ms")
