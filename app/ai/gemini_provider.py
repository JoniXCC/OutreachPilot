"""Google Gemini via the public REST API (free tier from Google AI Studio)."""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.provider import AIResponse, AIResponseError, HTTPProvider

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiProvider(HTTPProvider):
    name = "gemini"

    def __init__(self, api_key: str | None, model: str = "gemini-2.5-flash-lite", *,
                 temperature: float = 0.3, max_tokens: int = 600, timeout: float = 30.0,
                 client: httpx.Client | None = None) -> None:
        super().__init__(api_key, model, temperature, max_tokens, timeout, client)

    def generate_text(self, prompt: str, *, system: str | None = None,
                      temperature: float | None = None, max_tokens: int | None = None,
                      json_mode: bool = False) -> AIResponse:
        config: dict[str, Any] = {
            "temperature": self.default_temperature if temperature is None else temperature,
            "maxOutputTokens": max_tokens or self.default_max_tokens,
        }
        if json_mode:
            config["responseMimeType"] = "application/json"
        if "2.5-flash" in self.model:
            # "Thinking" tokens count against output quota; these tasks don't need them.
            config["thinkingConfig"] = {"thinkingBudget": 0}
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": config,
        }
        if system:
            payload["systemInstruction"] = {"parts": [{"text": system}]}

        data = self._post(GEMINI_URL.format(model=self.model), payload,
                          {"x-goog-api-key": self._api_key, "Content-Type": "application/json"})
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise AIResponseError(f"gemini returned no answer ({reason})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise AIResponseError(f"gemini returned empty text ({candidates[0].get('finishReason')})")
        usage = data.get("usageMetadata") or {}
        return AIResponse(text=text, provider=self.name, model=self.model,
                          prompt_tokens=int(usage.get("promptTokenCount", 0)),
                          completion_tokens=int(usage.get("candidatesTokenCount", 0)))
