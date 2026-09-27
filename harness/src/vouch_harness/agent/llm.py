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
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Protocol

import yaml

Message = dict[str, Any]


@dataclass(frozen=True)
class Reply:
    """One model turn: the assistant message exactly as the provider sent
    it, and why generation stopped ("stop", "tool_calls", "length", ...;
    None when unknown). finish_reason lives beside the message, not in
    it, because the message is sent back to the provider verbatim."""

    message: Message
    finish_reason: str | None = None


class ChatClient(Protocol):
    """Returns the assistant turn for a conversation so far."""

    def complete(
        self, messages: list[Message], tools: list[dict[str, Any]], sample: int
    ) -> Reply: ...


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


def cache_identity(config: ModelConfig) -> str:
    """Everything about a model that shapes its requests besides the
    conversation: endpoint, model id, and params as canonical JSON. A
    changed setting must miss the cache, not replay old responses.
    Empty params add nothing, so default-settings caches stay valid."""
    identity = f"{config.endpoint}|{config.model}"
    if config.params:
        params = json.dumps(
            config.params, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        identity += f"|{params}"
    return identity


_SEGMENT_RE = re.compile(r"[A-Za-z0-9_.-]+")


def check_path_segment(kind: str, value: object) -> str:
    """A config name that becomes one directory level of a run path
    (runs/<model>/<task>/s<n>). A "/" would add a level that discover()
    never globs, so the runs would silently vanish from labeling and
    scoring; "." and ".." would escape the level."""
    if not isinstance(value, str) or not _SEGMENT_RE.fullmatch(value) or value in {".", ".."}:
        raise ValueError(f"{kind} {value!r}: use letters, digits, '_', '.', '-' only")
    return value


def load_models(path: str | Path) -> dict[str, ModelConfig]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out = {}
    for name, spec in raw.items():
        # Only the config key is restricted; the provider's model id
        # ("vendor/model" on OpenRouter) is free-form.
        check_path_segment("model name", name)
        unknown = set(spec) - {"base_url", "model", "api_key_env", "rpm", "params"}
        if unknown:
            raise ValueError(f"model {name}: unknown keys {sorted(unknown)}")
        out[name] = ModelConfig(name=name, **spec)
    return out


MAX_RETRY_AFTER = 120.0  # seconds; a longer server request is capped, not obeyed


def retry_after_seconds(value: str, now: float) -> float | None:
    """Seconds to wait for a Retry-After header, in either RFC 9110 form
    (delay-seconds or an HTTP-date), clamped to [0, MAX_RETRY_AFTER].
    None when the header is unparsable, so the caller backs off instead."""
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - now
        except (TypeError, ValueError, IndexError):
            return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


class OpenAICompatClient:
    """POSTs to an OpenAI-compatible /chat/completions endpoint.

    Retries 429 and 5xx with exponential backoff (honoring Retry-After),
    and spaces requests to stay under the configured requests/minute.
    Transport failures (connection resets, timeouts, a body that is not
    JSON) are retried the same way, and every final failure is an
    LLMError, which the batch records against one run and moves past.
    """

    def __init__(
        self,
        config: ModelConfig,
        *,
        max_retries: int = 6,
        timeout: float = 120.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wallclock: Callable[[], float] = time.time,
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
        self._wallclock = wallclock  # only to read Retry-After HTTP-dates
        self._next_slot = 0.0

    def _throttle(self) -> None:
        now = self._clock()
        if now < self._next_slot:
            self._sleep(self._next_slot - now)
        self._next_slot = max(now, self._next_slot) + 60.0 / self._config.rpm

    def complete(self, messages: list[Message], tools: list[dict[str, Any]], sample: int) -> Reply:
        del sample  # distinct samples come from provider sampling; the cache keys on it
        body = {"model": self._config.model, "messages": messages, **self._config.params}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        payload = self._post(json.dumps(body).encode("utf-8"))
        try:
            choice = payload["choices"][0]
            message: Message = choice["message"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError(f"{self._config.name}: response has no message: {payload!r:.300}") from e
        if not isinstance(message, dict):
            raise LLMError(f"{self._config.name}: message is not an object: {message!r:.300}")
        reason = choice.get("finish_reason")
        return Reply(message, reason if isinstance(reason, str) else None)

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
                    header = e.headers.get("Retry-After")
                wait = retry_after_seconds(header, self._wallclock()) if header else None
                self._sleep(delay if wait is None else wait)
            except (OSError, http.client.HTTPException, ValueError) as e:
                # OSError covers URLError, resets, and timeouts; ValueError
                # covers a body that is not JSON (a proxy's HTML error page).
                if attempt == self._max_retries:
                    reason = e.reason if isinstance(e, urllib.error.URLError) else e
                    raise LLMError(f"{self._config.name}: {type(e).__name__}: {reason}") from e
                self._sleep(delay)
            delay = min(delay * 2, 60.0)
        raise AssertionError("unreachable: the last attempt returns or raises")


class CachedClient:
    """Wraps a client with a content-addressed on-disk response cache."""

    def __init__(self, inner: ChatClient, cache_dir: str | Path, identity: str) -> None:
        self._inner = inner
        self._dir = Path(cache_dir)
        self._identity = identity  # cache_identity(config); part of every key
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

    def complete(self, messages: list[Message], tools: list[dict[str, Any]], sample: int) -> Reply:
        key = self.key(messages, tools, sample)
        path = self._dir / key[:2] / f"{key}.json"
        if path.exists():
            self.hits += 1
            cached: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
            if "role" in cached:  # written before finish_reason was kept: a bare message
                return Reply(cached, None)
            return Reply(cached["message"], cached.get("finish_reason"))
        self.misses += 1
        reply = self._inner.complete(messages, tools, sample)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        entry = {"message": reply.message, "finish_reason": reply.finish_reason}
        tmp.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)  # atomic: an interrupted run never leaves half a response
        return reply
