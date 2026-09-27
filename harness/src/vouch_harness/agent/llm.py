"""Chat-completion clients for the real-agent evaluation.

One wire protocol covers most mainstream models: the OpenAI-compatible
Chat Completions API, which Gemini, OpenAI, Anthropic, DeepSeek, Groq,
OpenRouter, Ollama, and vLLM all serve. A model is therefore config
(base URL, key variable, model id), not code. Stdlib only: the request
is one POST, and a vendor SDK per provider is what this design avoids.

Every response is cached on disk under a key that hashes the complete
request plus a sample index. Re-running an evaluation costs nothing,
an interrupted run resumes where it stopped, and repeated samples of
the same prompt stay distinct.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

Message = dict[str, Any]


class ChatClient(Protocol):
    """Returns the assistant message for a conversation so far."""

    def complete(
        self, messages: list[Message], tools: list[dict[str, Any]], sample: int
    ) -> Message: ...


class LLMError(RuntimeError):
    """A provider call failed after retries, or returned no message."""


@dataclass(frozen=True)
class ModelConfig:
    """One model endpoint, loaded from eval/models.yaml."""

    name: str
    base_url: str
    model: str
    api_key_env: str
    rpm: float = 10.0  # client-side rate limit, requests per minute
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"


def load_models(path: str | Path) -> dict[str, ModelConfig]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = {}
    for name, spec in raw.items():
        unknown = set(spec) - {"base_url", "model", "api_key_env", "rpm", "params"}
        if unknown:
            raise ValueError(f"model {name}: unknown keys {sorted(unknown)}")
        out[name] = ModelConfig(name=name, **spec)
    return out


class OpenAICompatClient:
    """POSTs to an OpenAI-compatible /chat/completions endpoint.

    Retries 429 and 5xx with exponential backoff (honoring Retry-After),
    and spaces requests to stay under the configured requests/minute.
    """

    def __init__(
        self,
        config: ModelConfig,
        *,
        max_retries: int = 6,
        timeout: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        key = os.environ.get(config.api_key_env, "")
        if not key:
            raise LLMError(f"{config.name}: ${config.api_key_env} is not set")
        self._config = config
        self._key = key
        self._max_retries = max_retries
        self._timeout = timeout
        self._sleep = sleep
        self._clock = clock
        self._next_slot = 0.0

    def _throttle(self) -> None:
        now = self._clock()
        if now < self._next_slot:
            self._sleep(self._next_slot - now)
        self._next_slot = max(now, self._next_slot) + 60.0 / self._config.rpm

    def complete(
        self, messages: list[Message], tools: list[dict[str, Any]], sample: int
    ) -> Message:
        del sample  # distinct samples come from provider sampling; the cache keys on it
        body = {"model": self._config.model, "messages": messages, **self._config.params}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        payload = self._post(json.dumps(body).encode("utf-8"))
        try:
            message: Message = payload["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self._config.name}: response has no message: {payload!r:.300}") from e
        return message

    def _post(self, data: bytes) -> Any:
        delay = 2.0
        for attempt in range(self._max_retries + 1):
            self._throttle()
            req = urllib.request.Request(
                self._config.endpoint,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self._key}",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                # An HTTPError owns the open error response; release it
                # on every path, including the retries.
                with e:
                    if not (e.code == 429 or e.code >= 500) or attempt == self._max_retries:
                        detail = e.read().decode("utf-8", "replace")[:500]
                        raise LLMError(f"{self._config.name}: HTTP {e.code}: {detail}") from e
                    retry_after = e.headers.get("Retry-After")
                self._sleep(float(retry_after) if retry_after else delay)
            except urllib.error.URLError as e:
                if attempt == self._max_retries:
                    raise LLMError(f"{self._config.name}: {e.reason}") from e
                self._sleep(delay)
            delay = min(delay * 2, 60.0)
        raise AssertionError("unreachable: the last attempt returns or raises")


class CachedClient:
    """Wraps a client with a content-addressed on-disk response cache."""

    def __init__(self, inner: ChatClient, cache_dir: str | Path, identity: str) -> None:
        self._inner = inner
        self._dir = Path(cache_dir)
        self._identity = identity  # model endpoint and id; part of every key
        self.hits = 0
        self.misses = 0

    def key(self, messages: list[Message], tools: list[dict[str, Any]], sample: int) -> str:
        blob = json.dumps(
            {"id": self._identity, "messages": messages, "tools": tools, "sample": sample},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def complete(
        self, messages: list[Message], tools: list[dict[str, Any]], sample: int
    ) -> Message:
        key = self.key(messages, tools, sample)
        path = self._dir / key[:2] / f"{key}.json"
        if path.exists():
            self.hits += 1
            cached: Message = json.loads(path.read_text(encoding="utf-8"))
            return cached
        self.misses += 1
        message = self._inner.complete(messages, tools, sample)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(message, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # atomic: an interrupted run never leaves half a response
        return message
