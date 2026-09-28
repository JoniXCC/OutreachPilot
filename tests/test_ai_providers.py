"""AI provider tests - all HTTP is mocked with httpx.MockTransport (no network, no quota)."""

import json

import httpx
import pytest

from app.ai.gemini_provider import GeminiProvider
from app.ai.groq_provider import GroqProvider
from app.ai.mock_provider import MockProvider
from app.ai.provider import AIAuthError, AIConfigError, AIError, AIRateLimitError, AIResponseError
from app.ai.service import AIService, build_ai_service
from app.database.models import AIUsage
from app.utils.helpers import extract_json_object


def client_returning(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def gemini_ok(text: str):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-goog-api-key"] == "test-key"
        body = json.loads(request.content)
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 42, "candidatesTokenCount": 17},
        })
    return handler


def test_gemini_generate_json_and_usage():
    provider = GeminiProvider("test-key", client=client_returning(gemini_ok('{"a": 1}')))
    data, response = provider.generate_json("TASK: x", system="sys")
    assert data == {"a": 1}
    assert (response.prompt_tokens, response.completion_tokens) == (42, 17)


def test_groq_generate_json():
    def handler(request):
        assert request.headers["authorization"] == "Bearer gsk-test"
        assert json.loads(request.content)["response_format"] == {"type": "json_object"}
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}],
                                         "usage": {"prompt_tokens": 10, "completion_tokens": 3}})
    provider = GroqProvider("gsk-test", client=client_returning(handler))
    data, _ = provider.generate_json("hello json")
    assert data == {"ok": True}


def test_rate_limit_is_mapped():
    provider = GroqProvider("k", client=client_returning(
        lambda r: httpx.Response(429, headers={"retry-after": "12"}, json={"error": {"message": "slow"}})))
    with pytest.raises(AIRateLimitError) as exc:
        provider.generate_text("x")
    assert exc.value.retry_after == 12


def test_invalid_key_is_mapped():
    provider = GeminiProvider("bad", client=client_returning(
        lambda r: httpx.Response(400, json={"error": {"message": "API_KEY_INVALID"}})))
    with pytest.raises(AIAuthError):
        provider.generate_text("x")


def test_missing_key_raises_config_error():
    with pytest.raises(AIConfigError):
        GroqProvider(None)


def test_gemini_blocked_response():
    provider = GeminiProvider("test-key", client=client_returning(
        lambda r: httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})))
    with pytest.raises(AIResponseError):
        provider.generate_text("x")


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 2}\n```', {"a": 2}),
    ('Here you go: {"a": 3} hope it helps', {"a": 3}),
])
def test_extract_json_object(raw, expected):
    assert extract_json_object(raw) == expected


@pytest.mark.parametrize("raw", ["", "no json here", "[1, 2]", '{"a": '])
def test_extract_json_object_invalid(raw):
    with pytest.raises(ValueError):
        extract_json_object(raw)


# ------------------------------------------------------------------ AIService
class CountingProvider(MockProvider):
    cacheable = True  # behaves like a remote provider for cache tests

    def __init__(self, fail_with: Exception | None = None, responses: list[str] | None = None):
        super().__init__()
        self.calls = 0
        self.fail_with = fail_with
        self.responses = responses

    def generate_text(self, prompt, **kw):
        self.calls += 1
        if self.fail_with:
            raise self.fail_with
        if self.responses:
            from app.ai.provider import AIResponse
            return AIResponse(self.responses.pop(0), self.name, self.model, 5, 5)
        return super().generate_text(prompt, **kw)


def test_service_caches_results(session):
    provider = CountingProvider(responses=['{"x": 1}'])
    service = AIService(session, provider)
    assert service.generate_json("test", "TASK: nothing") == {"x": 1}
    assert service.generate_json("test", "TASK: nothing") == {"x": 1}
    assert provider.calls == 1
    usage = session.query(AIUsage).all()
    assert [u.cached for u in usage] == [False, True]


def test_service_bypasses_cache_when_asked(session):
    provider = CountingProvider(responses=['{"x": 1}', '{"x": 2}'])
    service = AIService(session, provider)
    service.generate_json("test", "TASK: nothing")
    assert service.generate_json("test", "TASK: nothing", use_cache=False) == {"x": 2}


def test_service_falls_back_on_rate_limit(session):
    primary = CountingProvider(fail_with=AIRateLimitError("quota"))
    fallback = CountingProvider(responses=['{"from": "fallback"}'])
    service = AIService(session, primary, fallback)
    assert service.generate_json("test", "TASK: nothing") == {"from": "fallback"}
    assert primary.calls == 1 and fallback.calls == 1


def test_service_repairs_invalid_json_once(session):
    provider = CountingProvider(responses=["not json", '{"fixed": true}'])
    assert AIService(session, provider).generate_json("t", "TASK: z") == {"fixed": True}
    assert provider.calls == 2


def test_service_raises_when_all_fail(session):
    service = AIService(session, CountingProvider(fail_with=AIError("down")))
    with pytest.raises(AIError):
        service.generate_json("t", "TASK: z")


def test_build_service_uses_mock_in_demo_without_keys(settings):
    service = build_ai_service(None, settings.model_copy(update={"ai_provider": "gemini"}))
    assert service.primary.name == "mock"


def test_build_service_requires_key_outside_demo(settings):
    with pytest.raises(AIConfigError):
        build_ai_service(None, settings.model_copy(update={"ai_provider": "groq", "demo_mode": False}))


def test_switching_provider_needs_no_code_change(settings):
    from pydantic import SecretStr
    s = settings.model_copy(update={"ai_provider": "groq", "groq_api_key": SecretStr("gsk_x")})
    assert build_ai_service(None, s).primary.name == "groq"
    s = settings.model_copy(update={"ai_provider": "gemini", "gemini_api_key": SecretStr("AIza_x")})
    assert build_ai_service(None, s).primary.name == "gemini"
