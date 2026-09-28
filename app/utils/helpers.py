"""Small, dependency-free helper functions (pure Python, no AI)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

FREE_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com",
    "yahoo.de", "icloud.com", "me.com", "aol.com", "gmx.de", "gmx.net", "web.de",
    "proton.me", "protonmail.com", "t-online.de", "mail.com", "yandex.com", "zoho.com",
})


def utcnow() -> datetime:
    """Naive UTC timestamp (SQLite stores naive datetimes)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_email(email: str | None) -> str:
    """Lower-case and trim an email address. Returns '' for empty input."""
    return (email or "").strip().lower()


def email_domain(email: str | None) -> str:
    email = normalize_email(email)
    return email.rsplit("@", 1)[1] if "@" in email else ""


def normalize_domain(url_or_domain: str | None) -> str:
    """Return the bare registrable-looking host: 'https://www.ABC.de/x' -> 'abc.de'."""
    value = (url_or_domain or "").strip().lower()
    if not value:
        return ""
    if "@" in value and "/" not in value:
        return email_domain(value)
    if "://" not in value:
        value = "http://" + value
    host = urlparse(value).hostname or ""
    return host[4:] if host.startswith("www.") else host


def normalize_website(url: str | None) -> str:
    """Normalise a website to 'https://host/path' form (or '' if empty)."""
    value = (url or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if not parsed.hostname:
        return ""
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{path}"


def normalize_name(value: str | None) -> str:
    """Case/whitespace/punctuation-insensitive key for company or person names."""
    value = (value or "").lower()
    value = re.sub(r"\b(gmbh|ltd|llc|inc|ag|ug|co|corp|limited|s\.?l\.?|bv)\b\.?", " ", value)
    value = re.sub(r"[^\w]+", " ", value)
    return " ".join(value.split())


def is_free_email_domain(domain: str) -> bool:
    return domain.lower() in FREE_EMAIL_DOMAINS


def first_name(full_name: str | None) -> str:
    parts = (full_name or "").strip().split()
    if not parts:
        return ""
    # Skip common titles ("Dr. Anna Schmidt" -> "Anna")
    titles = {"dr", "dr.", "prof", "prof.", "mr", "mr.", "mrs", "mrs.", "ms", "ms."}
    for part in parts:
        if part.lower() not in titles:
            return part.capitalize() if part.islower() else part
    return ""


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w[\w'-]*\b", text or ""))


def truncate(text: str | None, max_chars: int) -> str:
    text = text or ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 1)].rstrip() + "…"


def stable_hash(*parts: Any) -> str:
    raw = json.dumps(parts, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_json_object(text: str) -> dict[str, Any]:
    """Extract the first JSON object from an LLM response.

    Handles ```json fences and leading/trailing prose. Raises ValueError if no
    valid JSON object can be found.
    """
    if not text or not text.strip():
        raise ValueError("empty response")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL | re.IGNORECASE)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start == -1:
            raise ValueError("no JSON object found in response") from None
        decoder = json.JSONDecoder()
        try:
            value, _ = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise ValueError("JSON response is not an object")
    return value


def reply_subject(subject: str | None) -> str:
    subject = (subject or "").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def from_json(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default
