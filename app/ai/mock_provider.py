"""Deterministic offline provider.

Used by tests and by the keyless demo (``AI_PROVIDER=mock``). It reads the
``TASK:`` line and ``key: value`` fields of our prompts and returns plausible,
rule-based JSON - no network, no quota, fully reproducible.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.ai.provider import AIProvider, AIResponse
from app.utils.helpers import truncate


def parse_prompt(prompt: str) -> tuple[str, dict[str, str], str]:
    """Return (task, fields, quoted_block) from one of our prompts."""
    task_match = re.search(r"^TASK:\s*(\w+)", prompt, re.MULTILINE)
    task = task_match.group(1) if task_match else ""
    fields: dict[str, str] = {}
    for line in prompt.splitlines():
        m = re.match(r"^([a-z_]+):\s*(.*)$", line)
        if m and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2).strip()
    block = re.search(r'"""\n(.*?)\n"""', prompt, re.DOTALL)
    return task, fields, block.group(1) if block else ""


def _val(fields: dict[str, str], key: str, default: str = "") -> str:
    value = fields.get(key, "")
    return default if value in ("", "none") else value


class MockProvider(AIProvider):
    name = "mock"

    def __init__(self, model: str = "mock-1", temperature: float = 0.0, max_tokens: int = 600) -> None:
        super().__init__(model, temperature, max_tokens)

    def generate_text(self, prompt: str, *, system: str | None = None,
                      temperature: float | None = None, max_tokens: int | None = None,
                      json_mode: bool = False) -> AIResponse:
        task, fields, block = parse_prompt(prompt)
        handler = getattr(self, f"_task_{task}", None)
        payload: Any = handler(fields, block) if handler else {"text": "OK"}
        text = json.dumps(payload) if json_mode or handler else str(payload)
        return AIResponse(text=text, provider=self.name, model=self.model,
                          prompt_tokens=len(prompt) // 4, completion_tokens=len(text) // 4)

    # ------------------------------------------------------------------ tasks
    def _task_define_icp(self, f: dict[str, str], _: str) -> dict[str, Any]:
        industry = _val(f, "target_industry", "small businesses")
        return {
            "summary": truncate(f"{industry} ({_val(f, 'company_size', 'SMB')}) in "
                                f"{_val(f, 'locations', 'target regions')}, reached via "
                                f"{_val(f, 'roles', 'decision makers')}.", 240),
            "pain_points": ["Repetitive manual work", "Limited staff time", "Slow response to customers"],
            "qualifying_signals": ["Online booking or contact form", "Multiple locations", "Growing team"],
            "disqualifiers": ["Enterprise-sized organisation", "Outside target region"],
        }

    def _task_research_summary(self, f: dict[str, str], text: str) -> dict[str, Any]:
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n", text) if len(s.strip()) > 25]
        if not sentences:
            return {"company_summary": "", "relevant_signal": "", "personalization_angle": "",
                    "confidence": 0.2}
        keywords = ("appointment", "booking", "patients", "customers", "clients", "team",
                    "locations", "service", "support", "contact")
        signal = next((s for s in sentences[1:] if any(k in s.lower() for k in keywords)), "")
        return {
            "company_summary": truncate(sentences[0], 160),
            "relevant_signal": truncate(signal, 160),
            "personalization_angle": "Relate the offer to how they handle customer enquiries.",
            "confidence": 0.7 if len(text) > 300 else 0.45,
        }

    def _task_write_cold_email(self, f: dict[str, str], _: str) -> dict[str, Any]:
        company = _val(f, "recipient_company", "your company")
        signal = _val(f, "relevant_signal")
        summary = _val(f, "research_summary")
        product = _val(f, "product", "our product")
        if signal:
            opener = f"I was reading about {company} and noticed this: {signal.rstrip('.')}."
            reason = f"Used website signal: {truncate(signal, 60)}"
        elif summary:
            opener = f"I came across {company} while looking at {_val(f, 'industry', 'businesses')} in your area."
            reason = "Used company summary from website research."
        else:
            opener = f"I'm reaching out to {company} because of your role as {_val(f, 'recipient_role', 'a decision maker')}."
            reason = "Light personalisation: company name and role only (no research available)."
        body = (
            f"{opener}\n\n"
            f"We built {product}. {_val(f, 'value_proposition', '').rstrip('.')}. "
            f"The idea is to take repetitive work off your team's plate without changing how you "
            f"already operate, so people can focus on the conversations that actually need them.\n\n"
            f"{_val(f, 'call_to_action', 'Would a short call next week be useful?')}"
        )
        return {"subject": f"Quick question for {company}"[:70], "body": body,
                "personalization_reason": reason}

    def _task_write_followup(self, f: dict[str, str], _: str) -> dict[str, Any]:
        company = _val(f, "recipient_company", "your team")
        if _val(f, "is_final") == "true":
            body = (f"I'll keep this short and won't follow up again after this. If easing "
                    f"repetitive work at {company} becomes a priority later, just reply to this "
                    f"email and I'll send a short overview. Thanks for your time either way.")
        else:
            body = (f"Just following up in case my previous email got buried. Would reducing "
                    f"repetitive enquiries be worth exploring for {company}? Happy to send over "
                    f"a short example if that's easier than a call.")
        return {"body": body, "personalization_reason": "Follow-up referencing the original offer."}

    def _task_classify_reply(self, f: dict[str, str], text: str) -> dict[str, Any]:
        t = text.lower()
        rules: list[tuple[str, tuple[str, ...], float]] = [
            ("UNSUBSCRIBE", ("unsubscribe", "remove me", "stop emailing", "do not contact", "don't contact"), 0.97),
            ("OUT_OF_OFFICE", ("out of office", "on vacation", "on holiday", "away until", "back on",
                               "abwesend", "limited access to email"), 0.93),
            ("WRONG_PERSON", ("wrong person", "not the right person", "not responsible", "you should contact",
                              "please contact", "reach out to"), 0.85),
            ("NOT_INTERESTED", ("not interested", "no thanks", "no thank you", "not a fit", "we're good",
                                "already have", "not looking"), 0.9),
            ("INTERESTED", ("interested", "let's talk", "set up a call", "book a demo", "sounds good",
                            "happy to chat", "send me a time", "schedule"), 0.88),
            ("MORE_INFORMATION", ("more information", "more info", "send me details", "brochure",
                                  "tell me more", "pricing"), 0.8),
        ]
        for category, keywords, confidence in rules:
            if any(k in t for k in keywords):
                break
        else:
            category, confidence = ("QUESTION", 0.75) if "?" in t else ("UNCLEAR", 0.4)
        date_match = re.search(r"(20\d\d-\d\d-\d\d)", text)
        return {
            "category": category,
            "confidence": confidence,
            "summary": truncate(f"Reply looks like {category.lower().replace('_', ' ')}: "
                                f"{text.strip().splitlines()[0] if text.strip() else ''}", 140),
            "requires_human": category in {"INTERESTED", "MORE_INFORMATION", "QUESTION", "UNCLEAR"},
            "return_date": date_match.group(1) if date_match else None,
            "referral_contact": None,
        }

    def _task_draft_reply(self, f: dict[str, str], _: str) -> dict[str, Any]:
        category = _val(f, "category", "QUESTION")
        if category == "WRONG_PERSON":
            body = ("Thanks for letting me know, and sorry for the misdirected email. Could you "
                    "point me to the person who looks after this at "
                    f"{_val(f, 'recipient_company', 'your company')}? I'd really appreciate it.")
        else:
            body = (f"Thanks for getting back to me. Happy to share more about "
                    f"{_val(f, 'product', 'what we do')}: {_val(f, 'value_proposition', '').rstrip('.')}. "
                    f"I'll put together the specific details you asked about rather than guess here. "
                    f"Would a 15-minute call later this week work to walk you through it?")
        return {"body": body}
