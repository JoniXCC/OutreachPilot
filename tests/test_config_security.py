"""Configuration and secret-handling tests."""

import json
import logging

from pydantic import SecretStr

from app.config.logging_config import JsonFormatter, log_event, redact, setup_logging
from app.config.settings import Settings


def test_redacts_known_secret_formats():
    text = ("gemini AIzaSyA1234567890abcdefghijklmnop groq gsk_abcdefghijklmnopqrstuvwxyz12 "
            "token ya29.a0AfH6SMBxyz Authorization: Bearer abc.def.ghi api_key=hunter2")
    out = redact(text)
    for secret in ("AIzaSyA123", "gsk_abcdef", "ya29.a0", "abc.def.ghi", "hunter2"):
        assert secret not in out


def test_json_formatter_redacts_sensitive_fields():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
    record.event = "ai_call"
    record.fields = {"api_key": "super-secret", "nested": {"token": "abc"}, "provider": "groq"}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["api_key"] == "***" and payload["nested"]["token"] == "***"
    assert payload["provider"] == "groq"


def test_log_event_writes_json_lines(tmp_path):
    log_file = tmp_path / "app.log"
    import app.config.logging_config as lc
    lc._configured = False
    logger = logging.getLogger(lc.LOGGER_NAME)
    old_handlers = logger.handlers[:]
    logger.handlers.clear()
    try:
        setup_logging("INFO", log_file)
        log_event("email_sent", lead_id=1, api_key="AIzaSyA1234567890abcdefghijklmnop")
        for handler in logger.handlers:
            handler.flush()
        line = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert line["event"] == "email_sent" and line["lead_id"] == 1
        assert "AIza" not in json.dumps(line)
    finally:
        for handler in logger.handlers:
            handler.close()
        logger.handlers[:] = old_handlers
        lc._configured = False


def test_public_view_masks_secrets():
    s = Settings(_env_file=None, gemini_api_key=SecretStr("AIza-real-key"), api_token=SecretStr("t"))
    view = s.public_view()
    assert view["gemini_api_key"] == "configured" and view["groq_api_key"] == "not set"
    assert "AIza-real-key" not in json.dumps(view, default=str)


def test_empty_env_values_become_none(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("AI_FALLBACK_PROVIDER", "")
    s = Settings(_env_file=None)
    assert s.gemini_api_key is None and s.ai_fallback_provider is None


def test_safe_defaults():
    s = Settings(_env_file=None)
    assert s.demo_mode and s.safe_mode
    assert s.max_emails_per_day == 20 and s.min_seconds_between_emails == 90 and s.max_followups == 2
