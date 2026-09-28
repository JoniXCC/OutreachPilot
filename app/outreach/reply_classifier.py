"""Reply classification.

Cheap deterministic checks run first and short-circuit the LLM:

* bounces (mailer-daemon / DSN subjects)        -> BOUNCE, no AI call
* explicit opt-out wording ("unsubscribe", ...) -> UNSUBSCRIBE, no AI call
* auto-reply headers / subjects                 -> OUT_OF_OFFICE, no AI call
* empty text                                    -> UNCLEAR, no AI call

Only genuine human replies reach the model, with just the newest message
(quoted history stripped) and a one-line context.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.ai.prompts import classify_prompt
from app.ai.provider import AIError
from app.ai.service import AIService
from app.config.logging_config import get_logger
from app.database.models import ReplyCategory
from app.email.gmail_client import InboxMessage
from app.email.thread_manager import extract_return_date, is_auto_reply, is_bounce
from app.utils.helpers import extract_json_object, truncate

logger = get_logger("classifier")

C = ReplyCategory
ALWAYS_HUMAN = {C.INTERESTED, C.MORE_INFORMATION, C.QUESTION, C.UNCLEAR}

CATEGORY_ALIASES = {
    "OOO": C.OUT_OF_OFFICE, "OUT_OF_OFFICE_REPLY": C.OUT_OF_OFFICE, "AUTO_REPLY": C.OUT_OF_OFFICE,
    "AUTOREPLY": C.OUT_OF_OFFICE, "AWAY": C.OUT_OF_OFFICE,
    "UNSUBSCRIBED": C.UNSUBSCRIBE, "OPT_OUT": C.UNSUBSCRIBE, "REMOVE": C.UNSUBSCRIBE,
    "NOTINTERESTED": C.NOT_INTERESTED, "NO_INTEREST": C.NOT_INTERESTED, "REJECTED": C.NOT_INTERESTED,
    "MORE_INFO": C.MORE_INFORMATION, "INFO_REQUEST": C.MORE_INFORMATION, "INFORMATION_REQUEST": C.MORE_INFORMATION,
    "POSITIVE": C.INTERESTED, "MEETING_REQUEST": C.INTERESTED,
    "WRONG_CONTACT": C.WRONG_PERSON, "REFERRAL": C.WRONG_PERSON,
    "UNKNOWN": C.UNCLEAR, "OTHER": C.UNCLEAR, "NEUTRAL": C.UNCLEAR,
}

UNSUBSCRIBE_PATTERN = re.compile(
    r"\b(unsubscribe|remove me|take me off|opt[\s-]?out|stop (e-?mailing|contacting|sending)|"
    r"do not (contact|e-?mail)|don'?t (contact|e-?mail) me|no more e-?mails|"
    r"abmelden|austragen|keine weiteren (e-?mails|nachrichten))\b",
    re.IGNORECASE,
)


class Classification(BaseModel):
    category: ReplyCategory
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = ""
    requires_human: bool = False
    return_date: date | None = None
    referral_contact: str | None = None
    source: str = "ai"  # "ai" | "rule" | "fallback"


def _normalise_category(value: Any) -> ReplyCategory | None:
    key = re.sub(r"[\s\-]+", "_", str(value or "").strip().upper())
    if key in ReplyCategory.__members__ and key != "BOUNCE":  # BOUNCE is rule-only
        return ReplyCategory(key)
    return CATEGORY_ALIASES.get(key.replace("_", "")) or CATEGORY_ALIASES.get(key)


def _parse_confidence(value: Any) -> float:
    try:
        conf = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.0
    if conf > 1.0:
        conf = conf / 100.0
    return max(0.0, min(1.0, conf))


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "yes", "1"}


def _parse_date(value: Any) -> date | None:
    if not value or str(value).lower() in {"null", "none", ""}:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_classification(raw: dict[str, Any] | str) -> Classification:
    """Validate/normalise an LLM classification. Never raises.

    Anything malformed degrades to ``UNCLEAR`` with confidence 0 so it is routed
    to a human instead of triggering an automatic action.
    """
    try:
        data = extract_json_object(raw) if isinstance(raw, str) else dict(raw)
    except (ValueError, TypeError):
        return Classification(category=C.UNCLEAR, confidence=0.0, summary="Unparseable AI output",
                              requires_human=True, source="fallback")
    category = _normalise_category(data.get("category"))
    if category is None:
        return Classification(category=C.UNCLEAR, confidence=0.0,
                              summary=truncate(f"Unknown category {data.get('category')!r}", 200),
                              requires_human=True, source="fallback")
    referral = data.get("referral_contact")
    referral = None if not referral or str(referral).lower() in {"null", "none"} else truncate(str(referral), 200)
    return Classification(
        category=category,
        confidence=_parse_confidence(data.get("confidence")),
        summary=truncate(str(data.get("summary") or ""), 300),
        requires_human=_parse_bool(data.get("requires_human")) or category in ALWAYS_HUMAN,
        return_date=_parse_date(data.get("return_date")),
        referral_contact=referral,
    )


class ReplyClassifier:
    def __init__(self, ai: AIService) -> None:
        self.ai = ai

    def classify(self, message: InboxMessage, latest_text: str, our_subject: str,
                 today: date) -> Classification:
        # --- deterministic short-circuits (zero AI cost) -------------------
        if is_bounce(message):
            return Classification(category=C.BOUNCE, confidence=1.0, summary="Delivery failure (bounce)",
                                  source="rule")
        if UNSUBSCRIBE_PATTERN.search(latest_text or ""):
            return Classification(category=C.UNSUBSCRIBE, confidence=0.99, source="rule",
                                  summary="Recipient asked not to be contacted again")
        if is_auto_reply(message):
            return Classification(category=C.OUT_OF_OFFICE, confidence=0.95, source="rule",
                                  summary="Automatic out-of-office reply",
                                  return_date=extract_return_date(latest_text, today))
        if not (latest_text or "").strip():
            return Classification(category=C.UNCLEAR, confidence=0.0, summary="Empty reply",
                                  requires_human=True, source="rule")

        # --- AI for genuine human language ------------------------------------
        try:
            data = self.ai.generate_json(
                "classify_reply",
                classify_prompt(our_subject=our_subject, reply_text=latest_text, today=today),
                temperature=0.0, max_tokens=200,
            )
        except AIError as exc:
            logger.warning("Classification unavailable, routing to human: %s", exc)
            return Classification(category=C.UNCLEAR, confidence=0.0, requires_human=True,
                                  summary="AI unavailable - needs manual review", source="fallback")
        result = parse_classification(data)
        if result.category == C.OUT_OF_OFFICE and result.return_date is None:
            result.return_date = extract_return_date(latest_text, today)
        if result.return_date and result.return_date < today:
            result.return_date = None
        return result
