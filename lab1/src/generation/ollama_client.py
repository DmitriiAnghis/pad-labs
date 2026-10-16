"""Minimal Ollama HTTP client with timeouts and readable errors (no stack traces for the user)."""
from __future__ import annotations

import logging
import re
import time

import requests

from common.config import Settings

log = logging.getLogger("generation.ollama")

THINK_RE = re.compile(r"<think>.*?</think>", re.S)


class OllamaError(RuntimeError):
    pass


class OllamaClient:
    def __init__(self, settings: Settings) -> None:
        self.base_url = settings.ollama_url
        self.timeout = settings.ollama_timeout
        self.num_ctx = settings.llm_num_ctx
        self.default_temperature = settings.llm_temperature
        self.think = settings.llm_think

    def health(self) -> tuple[bool, str]:
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return True, f"{len(models)} models: {', '.join(models[:12])}"
        except requests.RequestException as exc:
            return False, f"{type(exc).__name__}"

    def chat(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 700,
        temperature: float | None = None,
        json_format: bool = False,
    ) -> dict:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self.default_temperature if temperature is None else temperature,
                "num_ctx": self.num_ctx,
                "num_predict": max_tokens,
                "seed": 42,
            },
        }
        if self.think is not None:
            payload["think"] = self.think
        if json_format:
            payload["format"] = "json"
        url = f"{self.base_url}/api/chat"
        start = time.perf_counter()
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
            if resp.status_code == 400 and "think" in payload and "think" in resp.text.lower():
                payload.pop("think")  # model without a thinking switch: retry without the flag
                resp = requests.post(url, json=payload, timeout=self.timeout)
        except requests.Timeout as exc:
            raise OllamaError(f"LLM timeout after {self.timeout:.0f}s (model {model}); increase OLLAMA_TIMEOUT or use a smaller model") from exc
        except requests.ConnectionError as exc:
            raise OllamaError(f"Ollama is not reachable at {self.base_url} (OLLAMA_URL). Start Ollama or check the address.") from exc
        if resp.status_code == 404:
            raise OllamaError(f"model '{model}' is not available in Ollama. Run: ollama pull {model}")
        if resp.status_code >= 400:
            raise OllamaError(f"Ollama error {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        text = (data.get("message") or {}).get("content", "")
        text = THINK_RE.sub("", text).strip()
        return {
            "text": text,
            "model": data.get("model", model),
            "prompt_tokens": data.get("prompt_eval_count"),
            "completion_tokens": data.get("eval_count"),
            "ms": (time.perf_counter() - start) * 1000,
        }
