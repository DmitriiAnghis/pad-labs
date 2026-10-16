"""Answer generation from the selected chunks.

* ``OllamaGenerator``     - LLM via Ollama with a prompt variant from prompts.py;
* ``ExtractiveGenerator`` - no LLM: picks the context sentences that best match the question
                            and cites them. A baseline ("is the LLM worth it?") and a fallback
                            that works on any machine.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from common.config import GenerationConfig, Settings
from generation.ollama_client import OllamaClient
from generation.prompts import REFUSAL, build_messages, is_refusal
from retrieval.retriever import Candidate
from retrieval.tokenize import tokenize

log = logging.getLogger("generation")

CITATION_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")


@dataclass
class GenerationResult:
    text: str
    refused: bool
    cited: list[int] = field(default_factory=list)
    model: str = ""
    ms: float = 0.0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


def build_context(cands: list[Candidate], max_chars: int) -> tuple[str, list[Candidate]]:
    """Numbered passages; whole chunks are added until the character budget is used."""
    parts: list[str] = []
    used: list[Candidate] = []
    total = 0
    for c in cands:
        ch = c.chunk
        block = f"[{len(used) + 1}] {ch['section']}\n{ch['text']}"
        if used and total + len(block) > max_chars:
            break
        if not used and len(block) > max_chars:
            block = block[:max_chars]
        parts.append(block)
        used.append(c)
        total += len(block) + 2
    return "\n\n".join(parts), used


def parse_citations(text: str, n_sources: int) -> list[int]:
    found: list[int] = []
    for m in CITATION_RE.finditer(text):
        for num in m.group(1).split(","):
            k = int(num.strip())
            if 1 <= k <= n_sources and k not in found:
                found.append(k)
    return found


class OllamaGenerator:
    def __init__(self, settings: Settings, cfg: GenerationConfig, client: OllamaClient | None = None) -> None:
        self.cfg = cfg
        self.client = client or OllamaClient(settings)
        self.model = cfg.model or settings.llm_model

    def generate(self, question: str, cands: list[Candidate], language: str) -> GenerationResult:
        context, used = build_context(cands, self.cfg.max_context_chars)
        messages = build_messages(self.cfg.prompt, question, context, language)
        reply = self.client.chat(self.model, messages, max_tokens=self.cfg.max_tokens)
        text = reply["text"]
        return GenerationResult(
            text=text,
            refused=is_refusal(text),
            cited=parse_citations(text, len(used)),
            model=reply["model"],
            ms=reply["ms"],
            prompt_tokens=reply.get("prompt_tokens"),
            completion_tokens=reply.get("completion_tokens"),
        )


SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9])|\n+")


class ExtractiveGenerator:
    """Top sentences by stemmed-token overlap with the question (no LLM)."""

    model = "extractive"

    def __init__(self, cfg: GenerationConfig, max_sentences: int = 3) -> None:
        self.cfg = cfg
        self.max_sentences = max_sentences

    def generate(self, question: str, cands: list[Candidate], language: str) -> GenerationResult:
        _, used = build_context(cands, self.cfg.max_context_chars)
        q = set(tokenize(question))
        scored = []
        for n, c in enumerate(used, start=1):
            for pos, sent in enumerate(SENT_RE.split(c.chunk["text"])):
                toks = set(tokenize(sent))
                if len(sent) < 25 or not toks:
                    continue
                overlap = len(q & toks) / (len(q) or 1)
                # prefer earlier passages (they were ranked higher) and earlier sentences
                scored.append((overlap - 0.02 * (n - 1) - 0.005 * pos, n, sent.strip()))
        scored.sort(key=lambda x: -x[0])
        picked = [s for s in scored[: self.max_sentences] if s[0] > 0.15]
        if not picked:
            return GenerationResult(text=REFUSAL.get(language, REFUSAL["en"]), refused=True, model=self.model)
        text = " ".join(f"{sent} [{n}]" for _, n, sent in picked)
        return GenerationResult(text=text, refused=False, cited=sorted({n for _, n, _ in picked}), model=self.model)


def create_generator(settings: Settings, cfg: GenerationConfig):
    if cfg.backend == "extractive":
        return ExtractiveGenerator(cfg)
    return OllamaGenerator(settings, cfg)
