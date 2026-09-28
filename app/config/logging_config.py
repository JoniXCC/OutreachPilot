"""Structured (JSON-lines) logging with secret redaction.

Every business event goes through :func:`log_event`, which writes one JSON
object per line to the log file. The dashboard's *Logs* page reads that file.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER_NAME = "outreach"

# Known event names (documented in README). Free-form names are allowed too.
EVENTS = {
    "lead_imported",
    "lead_duplicate_skipped",
    "company_researched",
    "research_failed",
    "email_generated",
    "email_approved",
    "email_rejected",
    "email_sent",
    "email_send_failed",
    "send_limit_reached",
    "reply_received",
    "reply_classified",
    "followup_scheduled",
    "followup_generated",
    "human_review_requested",
    "lead_suppressed",
    "ai_call",
    "error",
}

_SECRET_PATTERNS = [
    re.compile(r"AIza[0-9A-Za-z\-_]{20,}"),  # Google API keys
    re.compile(r"gsk_[0-9A-Za-z]{20,}"),  # Groq API keys
    re.compile(r"ya29\.[0-9A-Za-z\-_.]+"),  # Google OAuth access tokens
    re.compile(r"1//[0-9A-Za-z\-_]{20,}"),  # Google refresh tokens
    re.compile(r"(?i)(bearer\s+)[0-9A-Za-z\-_.=]+"),
    re.compile(r"(?i)((?:api[_-]?key|secret|password|token)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
]

_SENSITIVE_KEYS = {"api_key", "apikey", "password", "secret", "token", "access_token",
                   "refresh_token", "client_secret", "authorization"}


def redact(text: str) -> str:
    """Remove anything that looks like a credential from a string."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + "***", text)
        else:
            text = pattern.sub("***", text)
    return text


def _redact_value(key: str, value: Any) -> Any:
    if key.lower() in _SENSITIVE_KEYS:
        return "***"
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(k, v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(key, v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", None) or "log",
            "message": redact(record.getMessage()),
        }
        fields = getattr(record, "fields", None)
        if fields:
            payload.update({k: _redact_value(k, v) for k, v in fields.items()})
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "event", None)
        fields = getattr(record, "fields", None) or {}
        extra = " ".join(f"{k}={_redact_value(k, v)}" for k, v in fields.items())
        head = f"{record.levelname:<7} {event or record.name}"
        return redact(f"{head} | {record.getMessage()} {extra}".rstrip())


_configured = False


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Configure the application logger once (idempotent)."""
    global _configured
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level.upper())
    if _configured:
        return
    logger.propagate = False

    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter())
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)

    # Keep noisy third-party loggers quiet (and away from our secrets).
    for noisy in ("httpx", "httpcore", "googleapiclient", "google_auth_oauthlib", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def log_event(event: str, message: str = "", level: int = logging.INFO, **fields: Any) -> None:
    """Emit a structured business event, e.g. ``log_event("email_sent", lead_id=3)``."""
    get_logger("events").log(
        level, message or event, extra={"event": event, "fields": fields}
    )


def read_log_tail(log_file: Path, max_lines: int = 500) -> list[dict[str, Any]]:
    """Return the most recent JSON log records (newest first) for the dashboard."""
    if not log_file.exists():
        return []
    with log_file.open("r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()[-max_lines:]
    records: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
