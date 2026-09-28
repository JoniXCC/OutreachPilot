"""Groq (OpenAI-compatible chat completions API, generous free tier)."""

from __future__ import annotations

from typing import Any

import httpx

from app.ai.provider import AIResponse, AIResponseError, HTTPProvider

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


class GroqProvider(HTTPProvider):
    name = "groq"

    def __init__(self, api_key: str | None, model: str = "llama-3.1-8b-instant", *,
                 temperature: float = 0.3, max_tokens: int = 600, timeout: float = 30.0,
                 client: httpx.Client | None = None) -> None:
        super().__init__(api_key, model, temperature, max_tokens, timeout, client)

    def generate_text(self, prompt: str, *, system: str | None = None,
                      temperature: float | None = None, max_tokens: int | None = None,
                      json_mode: bool = False) -> AIResponse:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.default_temperature if temperature is None else temperature,
            "max_tokens": max_tokens or self.default_max_tokens,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        data = self._post(GROQ_URL, payload, {"Authorization": f"Bearer {self._api_key}"})
        choices = data.get("choices") or []
        text = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
        if not text:
            raise AIResponseError("groq returned an empty answer")
        usage = data.get("usage") or {}
        return AIResponse(text=text, provider=self.name, model=self.model,
                          prompt_tokens=int(usage.get("prompt_tokens", 0)),
                          completion_tokens=int(usage.get("completion_tokens", 0)))
