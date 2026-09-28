"""Compact prompt templates.

Token discipline:
* one shared short system prompt,
* ``key: value`` lines instead of prose,
* every variable is truncated before it is inserted,
* structured JSON output so no follow-up "please reformat" calls are needed,
* greetings, signatures and opt-out lines are added by Python, not generated.

Each prompt starts with ``TASK: <name>`` which keeps prompts self-describing
(and lets the offline mock provider respond sensibly).
"""

from __future__ import annotations

from datetime import date

from app.utils.helpers import truncate

SYSTEM_PROMPT = (
    "You are a careful B2B sales assistant. Use only the facts provided; never invent "
    "facts, numbers, clients or results. Reply with one valid JSON object and nothing else."
)

EMAIL_RULES = (
    "Rules: body 60-110 words; do NOT write a greeting or signature (added automatically); "
    "plain, natural language; no flattery or fake compliments; no case studies, statistics or "
    "numbers not given above; no exaggerated claims; no links; exactly one call to action (the "
    "given CTA, may be rephrased); if research is 'none', personalise lightly using only company "
    "name, role and industry. Subject: max 7 words, no clickbait, no ALL CAPS."
)


def _fields(**values: object) -> str:
    return "\n".join(f"{k}: {v if v not in (None, '') else 'none'}" for k, v in values.items())


def icp_prompt(product: str, description: str, value_prop: str, industry: str,
               company_size: str, locations: str, roles: str) -> str:
    return "\n".join([
        "TASK: define_icp",
        _fields(product=truncate(product, 120), description=truncate(description, 400),
                value_proposition=truncate(value_prop, 300), target_industry=industry,
                company_size=company_size, locations=locations, roles=roles),
        'Return JSON: {"summary": "<=40 words ideal customer profile", '
        '"pain_points": ["max 3 short items"], "qualifying_signals": ["max 3 website signals '
        'that indicate a good fit"], "disqualifiers": ["max 3"]}',
    ])


def research_prompt(company: str, url: str, product: str, value_prop: str, page_text: str) -> str:
    return "\n".join([
        "TASK: research_summary",
        _fields(we_sell=truncate(f"{product} - {value_prop}", 220), company=company, website=url),
        'website_text:\n"""',
        page_text,
        '"""',
        "Using ONLY website_text, return JSON: "
        '{"company_summary": "<=25 words", "relevant_signal": "one concrete fact from the text '
        'relevant to what we sell, or empty string", "personalization_angle": "<=20 words", '
        '"confidence": 0.0-1.0}. If the text is thin or unrelated use empty strings and confidence < 0.4.',
    ])


def cold_email_prompt(*, sender_company: str, product: str, value_prop: str, tone: str,
                      cta: str, pain_points: str, recipient_name: str, recipient_role: str,
                      recipient_company: str, industry: str, location: str,
                      research_summary: str, relevant_signal: str, angle: str) -> str:
    return "\n".join([
        "TASK: write_cold_email",
        _fields(sender_company=sender_company, product=truncate(product, 120),
                value_proposition=truncate(value_prop, 300), tone=tone, call_to_action=cta,
                typical_pain_points=truncate(pain_points, 200),
                recipient_name=recipient_name, recipient_role=recipient_role,
                recipient_company=recipient_company, industry=industry, location=location,
                research_summary=truncate(research_summary, 300),
                relevant_signal=truncate(relevant_signal, 200), personalization_angle=truncate(angle, 150)),
        EMAIL_RULES,
        'Return JSON: {"subject": "...", "body": "...", '
        '"personalization_reason": "which given fact you used, <=20 words"}',
    ])


def followup_prompt(*, followup_number: int, max_followups: int, product: str, value_prop: str,
                    cta: str, recipient_name: str, recipient_company: str,
                    previous_email: str) -> str:
    final = followup_number >= max_followups
    return "\n".join([
        "TASK: write_followup",
        _fields(followup_number=followup_number, is_final=str(final).lower(),
                product=truncate(product, 120), value_proposition=truncate(value_prop, 200),
                call_to_action=cta, recipient_name=recipient_name, recipient_company=recipient_company,
                previous_email=truncate(previous_email, 350)),
        "Rules: body 30-70 words; no greeting or signature; do NOT repeat sentences from "
        "previous_email; polite and low-pressure; no guilt-tripping or fake urgency; one simple "
        "question. If is_final is true, make it a brief last note saying you won't follow up again.",
        'Return JSON: {"body": "...", "personalization_reason": "<=15 words"}',
    ])


REPLY_CATEGORIES_HELP = (
    "INTERESTED (wants a demo/call/pricing/next step), MORE_INFORMATION (wants details before "
    "deciding), QUESTION (asks a specific question), NOT_INTERESTED, WRONG_PERSON (not "
    "responsible or refers to someone else), OUT_OF_OFFICE (absence/auto-reply), UNSUBSCRIBE "
    "(asks to stop or be removed), UNCLEAR"
)


def classify_prompt(*, our_subject: str, reply_text: str, today: date) -> str:
    return "\n".join([
        "TASK: classify_reply",
        _fields(today=today.isoformat(), our_email_subject=truncate(our_subject, 120)),
        'reply:\n"""',
        reply_text,
        '"""',
        f"Categories: {REPLY_CATEGORIES_HELP}.",
        'Return JSON: {"category": "ONE_CATEGORY", "confidence": 0.0-1.0, "summary": "<=20 words", '
        '"requires_human": true|false, "return_date": "YYYY-MM-DD or null", '
        '"referral_contact": "name/email of the right person if given, else null"}',
    ])


def reply_draft_prompt(*, category: str, product: str, value_prop: str, cta: str,
                       recipient_name: str, recipient_company: str, their_message: str) -> str:
    return "\n".join([
        "TASK: draft_reply",
        _fields(category=category, product=truncate(product, 120),
                value_proposition=truncate(value_prop, 250), call_to_action=cta,
                recipient_name=recipient_name, recipient_company=recipient_company),
        'their_message:\n"""',
        truncate(their_message, 1200),
        '"""',
        "Rules: body 30-110 words; no greeting or signature; answer only with facts given - if "
        "something is unknown (e.g. exact price) say you'll send details rather than inventing it. "
        "INTERESTED/MORE_INFORMATION/QUESTION: helpful answer + propose a short call. "
        "WRONG_PERSON: thank them in one sentence and ask who the right person is; no pitch.",
        'Return JSON: {"body": "..."}',
    ])
