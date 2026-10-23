"""Request tracing: one trace per question, one observation per pipeline stage and per LLM call.

    trace "rag-answer"                       question -> answer
      span "query"                           language, search queries
        generation "rewrite"                 LLM prompt -> JSON with translation / sub-queries
      span "search"  (one per search query)
        span "embed_query" / "retrieve" / "filter" / "rerank"
      span "fuse"                            only with several sub-queries
      generation "answer"                    LLM prompt with the context -> answer with [n]

The trace is always built: the web page shows its steps under the answer. When LANGFUSE_PUBLIC_KEY
and LANGFUSE_SECRET_KEY are set it is also sent to Langfuse through its public HTTP ingestion API
(plain requests, no SDK) from a background thread, so a slow or unreachable Langfuse never slows
down or breaks an answer: the failure is only logged.
Code that runs without a trace (evaluation, /search) gets no-op observations.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator, Optional

import requests

from common.config import Settings

log = logging.getLogger("tracing")

_current: contextvars.ContextVar[Optional["Trace"]] = contextvars.ContextVar("trace", default=None)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Observation:
    name: str
    type: str = "span"  # span | generation
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parent_id: Optional[str] = None
    start: float = field(default_factory=time.time)
    end: Optional[float] = None
    input: Any = None
    output: Any = None
    metadata: dict = field(default_factory=dict)
    level: str = "DEFAULT"  # DEFAULT | WARNING | ERROR (Langfuse levels)
    status: Optional[str] = None
    model: Optional[str] = None
    model_parameters: Optional[dict] = None
    usage: Optional[dict] = None
    depth: int = 0

    @property
    def ms(self) -> float:
        return ((self.end or time.time()) - self.start) * 1000.0


@dataclass
class Trace:
    name: str
    input: Any = None
    output: Any = None
    metadata: dict = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    start: float = field(default_factory=time.time)
    end: Optional[float] = None
    observations: list[Observation] = field(default_factory=list)
    url: Optional[str] = None
    _stack: list[Observation] = field(default_factory=list)

    def add(self, obs: Observation) -> Observation:
        if self._stack:
            obs.parent_id = self._stack[-1].id
            obs.depth = self._stack[-1].depth + 1
        self.observations.append(obs)
        return obs

    def steps(self) -> list[dict]:
        """Short form for the web page: name, nesting depth, duration, one-line summary."""
        out = []
        for o in self.observations:
            step = {"name": o.name, "type": o.type, "depth": o.depth, "ms": round(o.ms, 1), "summary": _summary(o.output)}
            if o.model:
                step["model"] = o.model
            if o.usage:
                step["tokens"] = o.usage
            if o.level != "DEFAULT":
                step["level"] = o.level
                step["status"] = o.status
            out.append(step)
        return out

    def to_dict(self) -> dict:
        return {"id": self.id, "url": self.url, "steps": self.steps()}


def _summary(value: Any, limit: int = 160) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        text = ", ".join(f"{k}: {_summary(v, 60)}" for k, v in value.items())
    elif isinstance(value, list):
        if value and isinstance(value[0], dict) and "section" in value[0]:  # ranked chunks: best first
            text = f"{len(value)} chunks: " + "; ".join(str(v["section"]) for v in value[:3])
        else:
            text = f"{len(value)} items" if value and not isinstance(value[0], str) else "; ".join(map(str, value))
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def current() -> Optional[Trace]:
    return _current.get()


@contextmanager
def trace(name: str, input: Any = None, metadata: Optional[dict] = None,
          on_end: Optional[Callable[[Trace], None]] = None) -> Iterator[Trace]:
    """Start a trace for this request; on_end gets it when the request is over (even after an error)."""
    tr = Trace(name, input=input, metadata=dict(metadata or {}))
    token = _current.set(tr)
    try:
        yield tr
    except Exception as exc:
        tr.metadata["error"] = f"{type(exc).__name__}: {exc}"
        tr.tags.append("error")
        raise
    finally:
        tr.end = time.time()
        _current.reset(token)
        if on_end is not None:
            on_end(tr)


@contextmanager
def span(name: str, input: Any = None, **metadata) -> Iterator[Observation]:
    """Time one pipeline stage. Set .output (and .metadata) on the yielded object."""
    obs = Observation(name, input=input, metadata=metadata)
    tr = _current.get()
    if tr is not None:
        tr.add(obs)
        tr._stack.append(obs)
    try:
        yield obs
    except Exception as exc:
        obs.level, obs.status = "ERROR", f"{type(exc).__name__}: {exc}"
        raise
    finally:
        obs.end = time.time()
        if tr is not None:
            tr._stack.pop()


def record_generation(name: str, model: str, input: Any, output: Any, start: float, *,
                      model_parameters: Optional[dict] = None, usage: Optional[dict] = None,
                      error: Optional[str] = None) -> None:
    """One LLM call (called by the Ollama client): prompt, answer, model, parameters, tokens."""
    tr = _current.get()
    if tr is None:
        return
    obs = Observation(name, type="generation", start=start, end=time.time(), input=input, output=output,
                      model=model, model_parameters=model_parameters, usage=usage)
    if error:
        obs.level, obs.status = "ERROR", error
    tr.add(obs)


# ---------------------------------------------------------------- Langfuse


class LangfuseExporter:
    """Sends finished traces to Langfuse: POST {host}/api/public/ingestion, HTTP Basic auth
    (public key = user, secret key = password). Keys come only from .env and are never logged."""

    def __init__(self, host: str, public_key: str, secret_key: str, timeout: float = 10.0) -> None:
        self.host = host.rstrip("/")
        self.auth = (public_key, secret_key)
        self.timeout = timeout
        self._project_id: Optional[str] = None
        self._project_checked = False
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> Optional["LangfuseExporter"]:
        if not (settings.langfuse_public_key and settings.langfuse_secret_key):
            return None
        return cls(settings.langfuse_host, settings.langfuse_public_key, settings.langfuse_secret_key)

    def project_id(self) -> Optional[str]:
        """Needed only for the trace link; asked once, a failure falls back to a link without it."""
        with self._lock:
            if not self._project_checked:
                self._project_checked = True
                try:
                    resp = requests.get(f"{self.host}/api/public/projects", auth=self.auth, timeout=3)
                    resp.raise_for_status()
                    data = resp.json().get("data") or []
                    self._project_id = data[0]["id"] if data else None
                except (requests.RequestException, ValueError, KeyError) as exc:
                    log.warning("Langfuse project lookup failed (%s): %s", self.host, type(exc).__name__)
            return self._project_id

    def trace_url(self, trace_id: str) -> str:
        pid = self.project_id()
        return f"{self.host}/project/{pid}/traces/{trace_id}" if pid else f"{self.host}/trace/{trace_id}"

    def send(self, tr: Trace) -> None:
        """Non-blocking: the answer is returned while the trace is being uploaded."""
        tr.url = self.trace_url(tr.id)
        threading.Thread(target=self._post, args=(self.events(tr),), name="langfuse-export", daemon=True).start()

    def _post(self, batch: list[dict]) -> None:
        try:
            resp = requests.post(f"{self.host}/api/public/ingestion", json={"batch": batch},
                                 auth=self.auth, timeout=self.timeout)
            if resp.status_code >= 400:
                log.warning("Langfuse rejected the trace: HTTP %s %s", resp.status_code, resp.text[:200])
                return
            errors = (resp.json() or {}).get("errors") if resp.content else None
            if errors:
                log.warning("Langfuse rejected %d events: %s", len(errors), str(errors)[:300])
        except (requests.RequestException, ValueError) as exc:
            log.warning("Langfuse is not reachable at %s: %s", self.host, type(exc).__name__)

    @staticmethod
    def events(tr: Trace) -> list[dict]:
        def event(kind: str, body: dict, ts: float) -> dict:
            return {"id": uuid.uuid4().hex, "timestamp": _iso(ts), "type": kind, "body": body}

        batch = [event("trace-create", {
            "id": tr.id, "timestamp": _iso(tr.start), "name": tr.name, "input": tr.input, "output": tr.output,
            "metadata": tr.metadata, "tags": tr.tags,
        }, tr.start)]
        for o in tr.observations:
            body = {
                "id": o.id, "traceId": tr.id, "parentObservationId": o.parent_id, "name": o.name,
                "startTime": _iso(o.start), "endTime": _iso(o.end or o.start), "input": o.input, "output": o.output,
                "metadata": o.metadata, "level": o.level, "statusMessage": o.status,
            }
            if o.type == "generation":
                body.update({"model": o.model, "modelParameters": o.model_parameters, "usage": o.usage})
            batch.append(event(f"{o.type}-create", body, o.start))
        return batch
