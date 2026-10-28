"""LLM clients (Groq + local Ollama) behind a content-addressed disk cache.

Reproducibility: every request is keyed by sha256(provider, model, messages,
temperature, seed, json flag). The first answer is stored under
``.cache/llm/<key>.json`` and replayed afterwards, so an LLM-backed metric gives
the same score for the same submission even if the remote model changes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import requests


class LLMError(RuntimeError):
    """``cacheable`` marks deterministic model-side failures (e.g. a generation aborted by the model);
    caching them keeps grades reproducible. Network/auth/rate-limit errors are never cached."""

    def __init__(self, msg: str, cacheable: bool = False):
        super().__init__(msg)
        self.cacheable = cacheable


_ERR_PREFIX = "__llm_error__:"


def load_dotenv(base_dir: Path):
    env_file = Path(base_dir) / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class LLMCache:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @staticmethod
    def key(payload: dict) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def get(self, key: str):
        p = self.dir / f"{key}.json"
        if p.exists():
            return json.loads(p.read_text())["response"]
        return None

    def put(self, key: str, payload: dict, response: str):
        with self._lock:
            (self.dir / f"{key}.json").write_text(json.dumps({"request": payload, "response": response}, indent=1))


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def parse_json(text: str):
    """Parse a JSON object from a model reply, tolerating code fences / chatter."""
    text = _strip_think(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group())
        raise


class _Client:
    provider = ""

    def __init__(self, cache: LLMCache, model: str):
        self.cache, self.model = cache, model

    def chat(self, messages: list[dict], temperature: float = 0, seed: int = 42,
             json_mode: bool = False, max_tokens: int = 1024) -> tuple[str, bool]:
        """Returns (text, from_cache)."""
        payload = {"provider": self.provider, "model": self.model, "messages": messages,
                   "temperature": temperature, "seed": seed, "json": json_mode}
        key = self.cache.key(payload)
        hit = self.cache.get(key)
        if hit is not None:
            if hit.startswith(_ERR_PREFIX):
                raise LLMError(hit[len(_ERR_PREFIX):] + " (cached)", cacheable=True)
            return hit, True
        if not self.available:
            # checked only on a cache miss: cached answers keep grades identical on machines
            # where the service is down or not installed (e.g. a Linux grading server without Ollama)
            raise LLMError(f"{self.provider} is not available")
        try:
            text = self._call(messages, temperature, seed, json_mode, max_tokens)
        except LLMError as exc:
            if exc.cacheable:
                self.cache.put(key, payload, _ERR_PREFIX + str(exc))
            raise
        self.cache.put(key, payload, text)
        return text, False

    @property
    def available(self) -> bool:
        return True

    def _call(self, messages, temperature, seed, json_mode, max_tokens) -> str:  # pragma: no cover
        raise NotImplementedError


class GroqClient(_Client):
    provider = "groq"
    URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, cache: LLMCache, model: str = "openai/gpt-oss-120b"):
        super().__init__(cache, model)
        self.api_key = os.environ.get("GROQ_API_KEY", "")

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _call(self, messages, temperature, seed, json_mode, max_tokens):
        if not self.api_key:
            raise LLMError("GROQ_API_KEY is not set")
        body = {"model": self.model, "messages": messages, "temperature": temperature,
                "seed": seed, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            r = requests.post(self.URL, json=body, timeout=60,
                              headers={"Authorization": f"Bearer {self.api_key}"})
        except requests.RequestException as exc:
            raise LLMError(f"Groq request failed: {exc}") from exc
        if r.status_code != 200:
            # 400 = the model/request itself failed (deterministic); 401/429/5xx are transient
            raise LLMError(f"Groq HTTP {r.status_code}: {r.text[:300]}", cacheable=r.status_code == 400)
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LLMError("Groq reply truncated (max_tokens too small)")
        return choice["message"]["content"]


class OllamaClient(_Client):
    """Local Ollama. qwen3 builds may keep "thinking" even with think=false, so callers use
    JSON mode: grammar-constrained decoding makes the model answer directly and quickly."""
    provider = "ollama"

    def __init__(self, cache: LLMCache, model: str = "qwen3:4b", url: str | None = None):
        super().__init__(cache, model)
        self.url = (url or os.environ.get("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self._available = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                tags = requests.get(f"{self.url}/api/tags", timeout=3).json()
                self._available = any(m.get("name") == self.model for m in tags.get("models", []))
            except (requests.RequestException, ValueError):
                self._available = False
        return self._available

    def _call(self, messages, temperature, seed, json_mode, max_tokens):
        body = {"model": self.model, "messages": messages, "stream": False, "think": False,
                "options": {"temperature": temperature, "seed": seed, "num_predict": max_tokens}}
        if json_mode:
            body["format"] = "json"
        try:
            r = requests.post(f"{self.url}/api/chat", json=body, timeout=180)
        except requests.RequestException as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200 or "error" in data:
            err = data.get("error") or r.text[:300]
            raise LLMError(f"Ollama HTTP {r.status_code}: {err}", cacheable="prediction aborted" in err)
        return _strip_think(data["message"]["content"])
