"""AIService: the only entry point application code uses for LLM calls.

Adds, on top of a raw provider:
* a persistent response cache (SQLite) - identical prompts never cost quota twice,
* automatic fallback to a second free provider on rate limits / outages,
* one "repair" retry when a model returns invalid JSON,
* per-call usage records (tokens, cache hits) for the dashboard cost panel.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.ai.gemini_provider import GeminiProvider
from app.ai.groq_provider import GroqProvider
from app.ai.mock_provider import MockProvider
from app.ai.prompts import SYSTEM_PROMPT
from app.ai.provider import (
    AIAuthError, AIConfigError, AIError, AIProvider, AIRateLimitError, AIResponse, AIResponseError,
)
from app.config.logging_config import get_logger, log_event
from app.config.settings import Settings
from app.database.models import AICacheEntry, AIUsage
from app.utils.helpers import extract_json_object, stable_hash

logger = get_logger("ai")


def create_provider(name: str, settings: Settings, client: httpx.Client | None = None) -> AIProvider:
    """Instantiate a provider by name (raises AIConfigError if its key is missing)."""
    common = {"temperature": settings.ai_temperature, "max_tokens": settings.ai_max_output_tokens}
    if name == "gemini":
        key = settings.gemini_api_key.get_secret_value() if settings.gemini_api_key else None
        return GeminiProvider(key, settings.gemini_model, timeout=settings.ai_timeout_seconds,
                              client=client, **common)
    if name == "groq":
        key = settings.groq_api_key.get_secret_value() if settings.groq_api_key else None
        return GroqProvider(key, settings.groq_model, timeout=settings.ai_timeout_seconds,
                            client=client, **common)
    if name == "mock":
        return MockProvider()
    raise AIConfigError(f"Unknown AI provider '{name}' (use gemini, groq or mock)")


class AIService:
    def __init__(self, session: Session | None, primary: AIProvider,
                 fallback: AIProvider | None = None, cache_enabled: bool = True) -> None:
        self.session = session
        self.primary = primary
        self.fallback = fallback
        self.cache_enabled = cache_enabled and session is not None

    @property
    def provider_label(self) -> str:
        label = f"{self.primary.name}:{self.primary.model}"
        return f"{label} (fallback {self.fallback.name})" if self.fallback else label

    # ------------------------------------------------------------------ public API
    def generate_json(self, purpose: str, prompt: str, *, system: str = SYSTEM_PROMPT,
                      use_cache: bool = True, temperature: float | None = None,
                      max_tokens: int | None = None) -> dict[str, Any]:
        cache_key = self._cache_key(purpose, prompt, system, temperature)
        if use_cache:
            cached = self._cache_get(cache_key)
            if cached is not None:
                try:
                    data = json.loads(cached)
                    self._record(self.primary, purpose, None, cached=True)
                    return data
                except json.JSONDecodeError:
                    pass

        errors: list[str] = []
        for provider in self._providers():
            try:
                data, response = self._json_with_repair(provider, prompt, system, temperature, max_tokens)
            except AIConfigError as exc:
                errors.append(str(exc))
                continue
            except AIAuthError as exc:
                self._record(provider, purpose, None, error=str(exc))
                errors.append(str(exc))
                continue
            except AIRateLimitError as exc:
                self._record(provider, purpose, None, error="rate limited")
                log_event("error", str(exc), level=30, provider=provider.name, purpose=purpose)
                errors.append(str(exc))
                continue
            except AIError as exc:
                self._record(provider, purpose, None, error=str(exc)[:200])
                errors.append(str(exc))
                continue
            self._record(provider, purpose, response)
            self._cache_put(cache_key, purpose, json.dumps(data, ensure_ascii=False))
            return data
        raise AIError("; ".join(errors) or "no AI provider available")

    def generate_text(self, purpose: str, prompt: str, *, system: str | None = None,
                      temperature: float | None = None, max_tokens: int | None = None) -> str:
        errors: list[str] = []
        for provider in self._providers():
            try:
                response = provider.generate_text(prompt, system=system, temperature=temperature,
                                                  max_tokens=max_tokens)
            except AIError as exc:
                errors.append(str(exc))
                continue
            self._record(provider, purpose, response)
            return response.text
        raise AIError("; ".join(errors) or "no AI provider available")

    # ------------------------------------------------------------------ internals
    def _providers(self) -> list[AIProvider]:
        return [p for p in (self.primary, self.fallback) if p is not None]

    @staticmethod
    def _json_with_repair(provider: AIProvider, prompt: str, system: str,
                          temperature: float | None, max_tokens: int | None
                          ) -> tuple[dict[str, Any], AIResponse]:
        try:
            return provider.generate_json(prompt, system=system, temperature=temperature,
                                          max_tokens=max_tokens)
        except AIResponseError:
            # One cheap retry with a stricter instruction, deterministic temperature.
            response = provider.generate_text(
                prompt + "\nIMPORTANT: output ONLY a single valid JSON object.",
                system=system, temperature=0.0, max_tokens=max_tokens, json_mode=True,
            )
            try:
                return extract_json_object(response.text), response
            except ValueError as exc:
                raise AIResponseError(f"{provider.name}: invalid JSON after retry") from exc

    def _cache_key(self, purpose: str, prompt: str, system: str, temperature: float | None) -> str:
        return stable_hash(self.primary.name, self.primary.model, purpose, system, prompt, temperature)

    def _cache_get(self, key: str) -> str | None:
        if not self.cache_enabled or self.session is None:
            return None
        entry = self.session.get(AICacheEntry, key)
        return entry.response if entry else None

    def _cache_put(self, key: str, purpose: str, response: str) -> None:
        if not self.cache_enabled or self.session is None:
            return
        entry = self.session.get(AICacheEntry, key)
        if entry:
            entry.response = response
        else:
            self.session.add(AICacheEntry(key=key, purpose=purpose, response=response))
        self.session.flush()

    def _record(self, provider: AIProvider, purpose: str, response: AIResponse | None,
                cached: bool = False, error: str | None = None) -> None:
        prompt_tokens = response.prompt_tokens if response else 0
        completion_tokens = response.completion_tokens if response else 0
        log_event("ai_call", provider=provider.name, model=provider.model, purpose=purpose,
                  prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                  cached=cached, success=error is None)
        if self.session is not None:
            self.session.add(AIUsage(provider=provider.name, model=provider.model, purpose=purpose,
                                     prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                                     cached=cached, success=error is None, error=error))
            self.session.flush()


def build_ai_service(session: Session | None, settings: Settings,
                     client: httpx.Client | None = None) -> AIService:
    """Create the configured service. Falls back to the mock provider only when
    no real provider can be built *and* demo mode is on."""
    primary: AIProvider | None = None
    fallback: AIProvider | None = None
    try:
        primary = create_provider(settings.ai_provider, settings, client)
    except AIConfigError as exc:
        logger.warning("Primary AI provider unavailable: %s", exc)
    if settings.ai_fallback_provider and settings.ai_fallback_provider != settings.ai_provider:
        try:
            fallback = create_provider(settings.ai_fallback_provider, settings, client)
        except AIConfigError as exc:
            logger.info("Fallback AI provider unavailable: %s", exc)
    if primary is None and fallback is not None:
        primary, fallback = fallback, None
    if primary is None:
        if settings.demo_mode:
            logger.warning("No AI API key configured - using the offline mock provider (demo mode).")
            primary = MockProvider()
        else:
            raise AIConfigError(
                "No AI provider configured. Set GEMINI_API_KEY or GROQ_API_KEY in .env "
                "(or AI_PROVIDER=mock for an offline demo)."
            )
    return AIService(session, primary, fallback, settings.ai_cache_enabled)
