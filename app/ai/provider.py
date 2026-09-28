"""Provider-agnostic AI interface.

Application code depends only on :class:`AIProvider`; switching
``AI_PROVIDER=gemini`` to ``AI_PROVIDER=groq`` requires no code changes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import httpx

from app.utils.helpers import extract_json_object


# --------------------------------------------------------------------------- errors
class AIError(RuntimeError):
    """Base class for all AI provider failures."""


class AIConfigError(AIError):
    """Provider is not configured (e.g. missing API key)."""


class AIAuthError(AIError):
    """API key rejected."""


class AIRateLimitError(AIError):
    """Free-tier quota / rate limit hit."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AIResponseError(AIError):
    """Provider answered but the content is unusable (empty, blocked, invalid JSON)."""


@dataclass
class AIResponse:
    text: str
    provider: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


# --------------------------------------------------------------------------- interface
class AIProvider(ABC):
    name: str = "base"

    def __init__(self, model: str, temperature: float = 0.3, max_tokens: int = 600) -> None:
        self.model = model
        self.default_temperature = temperature
        self.default_max_tokens = max_tokens

    @abstractmethod
    def generate_text(self, prompt: str, *, system: str | None = None,
                      temperature: float | None = None, max_tokens: int | None = None,
                      json_mode: bool = False) -> AIResponse:
        """Return the model's text completion for a single-turn prompt."""

    def generate_json(self, prompt: str, *, system: str | None = None,
                      temperature: float | None = None,
                      max_tokens: int | None = None) -> tuple[dict[str, Any], AIResponse]:
        """Return a parsed JSON object (raises :class:`AIResponseError` if invalid)."""
        response = self.generate_text(prompt, system=system, temperature=temperature,
                                      max_tokens=max_tokens, json_mode=True)
        try:
            return extract_json_object(response.text), response
        except ValueError as exc:
            raise AIResponseError(f"{self.name} returned invalid JSON: {exc}") from exc


class HTTPProvider(AIProvider):
    """Shared HTTP plumbing: timeouts, one retry on transient errors, error mapping."""

    max_retries = 1

    def __init__(self, api_key: str | None, model: str, temperature: float = 0.3,
                 max_tokens: int = 600, timeout: float = 30.0,
                 client: httpx.Client | None = None) -> None:
        super().__init__(model, temperature, max_tokens)
        if not api_key:
            raise AIConfigError(f"{self.name}: API key is not set (check your .env file)")
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=timeout)

    def _post(self, url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.post(url, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    time.sleep(1.5)
                    continue
                raise AIError(f"{self.name}: network error ({type(exc).__name__})") from exc
            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                raise AIRateLimitError(
                    f"{self.name}: rate limit / free-tier quota reached "
                    f"(retry after {retry_after or 'a while'}s). Consider AI_FALLBACK_PROVIDER.",
                    retry_after,
                )
            if resp.status_code in (401, 403) or (resp.status_code == 400 and "API_KEY" in resp.text):
                raise AIAuthError(f"{self.name}: API key rejected (HTTP {resp.status_code}). Check your .env.")
            if resp.status_code >= 500 and attempt < self.max_retries:
                last_exc = AIError(f"HTTP {resp.status_code}")
                time.sleep(1.5)
                continue
            if resp.status_code >= 400:
                raise AIError(f"{self.name}: HTTP {resp.status_code}: {_short_error(resp)}")
            try:
                return resp.json()
            except ValueError as exc:
                raise AIResponseError(f"{self.name}: non-JSON HTTP response") from exc
        raise AIError(f"{self.name}: request failed: {last_exc}")


def _parse_retry_after(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _short_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
        err = data.get("error", data)
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return str(msg)[:200]
    except ValueError:
        return resp.text[:200]
