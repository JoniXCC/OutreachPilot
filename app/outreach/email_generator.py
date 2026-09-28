"""Personalised email generation with deterministic quality gates.

The AI writes only the core body + subject. Python then:
* strips any greeting/signature the model added anyway,
* checks the text against the house rules (length, placeholders, hype, invented
  numbers, links, multiple CTAs),
* retries once with feedback if a *serious* rule is broken, otherwise falls back
  to a safe template,
* adds greeting, signature and opt-out line itself,
* stores the result as a draft awaiting approval (SAFE MODE) or auto-approved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.ai.prompts import cold_email_prompt, followup_prompt, reply_draft_prompt
from app.ai.provider import AIError
from app.ai.service import AIService
from app.config.logging_config import get_logger, log_event
from app.config.settings import Settings
from app.database.models import (
    Campaign, EmailMessage, Lead, LeadStatus, MessageKind, MessageStatus, Reply,
)
from app.database.repositories import MessageRepository
from app.outreach.compliance import check_can_contact
from app.outreach.status_machine import can_transition_lead
from app.utils.helpers import first_name, reply_subject, truncate, utcnow, word_count

logger = get_logger("email_generator")

OPT_OUT_LINE = "P.S. If this isn't relevant, just reply \"unsubscribe\" and I won't contact you again."

HYPE_PHRASES = ("revolutionary", "game-changer", "game changer", "cutting-edge", "cutting edge",
                "world-class", "best-in-class", "skyrocket", "guarantee", "10x", "unparalleled",
                "i was impressed", "i love what", "amazing work", "incredible", "blown away")
CLAIM_PHRASES = ("case study", "our clients", "companies like yours have", "helped companies",
                 "customers such as", "trusted by", "leading clinics", "leading companies")
_PLACEHOLDER = re.compile(r"\[[^\]]{1,40}\]|\{\{?[^}]{1,40}\}?\}|<[A-Z_ ]{3,30}>")
_GREETING = re.compile(r"^\s*(hi|hello|hey|dear|good (morning|afternoon))\b[^\n]{0,40},?\s*\n+", re.I)
_SIGNOFF = re.compile(r"\n\s*(best|kind regards|regards|best regards|cheers|thanks|thank you|"
                      r"sincerely|many thanks)[,!.]?\s*(\n.*)?$", re.I | re.S)
_NUMBER = re.compile(r"\b\d+(?:[.,]\d+)?\s*%|\b\d{2,}\b")

SERIOUS_FLAGS = {"placeholder", "empty", "too_long", "unverified_number", "link"}


@dataclass
class QualityReport:
    flags: list[str] = field(default_factory=list)

    @property
    def serious(self) -> bool:
        return any(f in SERIOUS_FLAGS for f in self.flags)


def clean_ai_body(body: str) -> str:
    """Remove a greeting / sign-off the model added despite instructions."""
    body = (body or "").replace("\r\n", "\n").strip()
    body = _GREETING.sub("", body, count=1)
    body = _SIGNOFF.sub("", body).strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    return body


def check_quality(subject: str | None, body: str, source_facts: str,
                  min_words: int = 40, max_words: int = 150) -> QualityReport:
    """Deterministic rule checks - no AI needed to police the AI."""
    report = QualityReport()
    text = f"{subject or ''}\n{body}"
    lower = text.lower()
    words = word_count(body)
    if not body.strip() or (subject is not None and not subject.strip()):
        report.flags.append("empty")
    if words < min_words:
        report.flags.append("too_short")
    if words > max_words:
        report.flags.append("too_long")
    if _PLACEHOLDER.search(text):
        report.flags.append("placeholder")
    if re.search(r"https?://|www\.", lower):
        report.flags.append("link")
    if any(p in lower for p in HYPE_PHRASES):
        report.flags.append("hype_language")
    if any(p in lower for p in CLAIM_PHRASES):
        report.flags.append("unverified_claim")
    source = source_facts.lower()
    for number in _NUMBER.findall(text):
        if number.strip().lower() not in source:
            report.flags.append("unverified_number")
            break
    if body.count("?") > 2:
        report.flags.append("multiple_ctas")
    if subject is not None and (len(subject) > 80 or (subject.isupper() and len(subject) > 4)
                                or "!!" in subject):
        report.flags.append("bad_subject")
    return report


def compose_email(body: str, contact_name: str, settings: Settings, campaign: Campaign,
                  include_opt_out: bool) -> str:
    """Wrap the core body with greeting, signature and optional opt-out line."""
    name = first_name(contact_name)
    greeting = f"Hi {name}," if name else "Hi there,"
    sender = campaign.sender_name or settings.sender_name
    signature_lines = [sender]
    if settings.sender_title:
        signature_lines.append(f"{settings.sender_title}, {campaign.company_name}")
    else:
        signature_lines.append(campaign.company_name)
    parts = [greeting, body.strip(), "Best,\n" + "\n".join(signature_lines)]
    if include_opt_out:
        parts.append(OPT_OUT_LINE)
    return "\n\n".join(parts)


def approval_state(settings: Settings, campaign: Campaign, kind: str,
                   quality: QualityReport | None = None) -> tuple[str, str | None]:
    """SAFE MODE -> human approval. AUTOMATIC only if globally allowed, enabled on the
    campaign, the email has no quality warnings, and it is not a conversational reply."""
    automatic = (not settings.safe_mode and campaign.auto_send
                 and kind in (MessageKind.INITIAL, MessageKind.FOLLOWUP)
                 and not (quality and quality.flags))
    return (MessageStatus.APPROVED, "auto") if automatic else (MessageStatus.PENDING_APPROVAL, None)


class EmailGenerator:
    def __init__(self, session: Session, settings: Settings, ai: AIService) -> None:
        self.session = session
        self.settings = settings
        self.ai = ai
        self.messages = MessageRepository(session)

    # ------------------------------------------------------------------ initial emails
    def generate_initial(self, lead: Lead, regenerate: bool = False) -> EmailMessage | None:
        campaign = lead.campaign
        decision = check_can_contact(self.session, lead, campaign, MessageKind.INITIAL)
        if not decision.allowed:
            logger.info("Skipping email for lead %s: %s", lead.id, decision.reason)
            return None
        open_drafts = self.messages.open_drafts_for_lead(lead.id, [MessageKind.INITIAL])
        if open_drafts and not regenerate:
            return open_drafts[0]
        for draft in open_drafts:
            draft.status = MessageStatus.CANCELLED
            draft.error = "replaced by regenerated draft"

        research = lead.research
        facts = " ".join([campaign.product_name, campaign.product_description, campaign.value_proposition,
                          campaign.call_to_action, lead.company_name, lead.location, lead.research_summary or ""])
        prompt = cold_email_prompt(
            sender_company=campaign.company_name, product=campaign.product_name,
            value_prop=campaign.value_proposition, tone=campaign.tone, cta=campaign.call_to_action,
            pain_points="; ".join(campaign.icp.get("pain_points", [])),
            recipient_name=first_name(lead.contact_name), recipient_role=lead.contact_role,
            recipient_company=lead.company_name, industry=lead.industry, location=lead.location,
            research_summary=research.get("company_summary", ""),
            relevant_signal=research.get("relevant_signal", "") if research.get("confidence", 0) >= 0.4 else "",
            angle=research.get("personalization_angle", ""),
        )

        subject, body, reason, quality, ai_generated = self._generate_with_gate(prompt, facts, regenerate)
        if not ai_generated:
            subject, body, reason = self._template_initial(lead, campaign)
            quality = check_quality(subject, body, facts)
            quality.flags.append("template_fallback")

        status, approved_by = approval_state(self.settings, campaign, MessageKind.INITIAL, quality)
        message = EmailMessage(
            lead_id=lead.id, campaign_id=campaign.id, kind=MessageKind.INITIAL, sequence=0,
            to_email=lead.email, subject=truncate(subject, 200),
            body=compose_email(body, lead.contact_name, self.settings, campaign,
                               self.settings.include_opt_out_line),
            personalization_reason=truncate(reason, 300), quality_flags=",".join(quality.flags),
            ai_generated=ai_generated, status=status, approved_by=approved_by,
            approved_at=utcnow() if approved_by else None,
        )
        self.session.add(message)
        if can_transition_lead(lead.status, LeadStatus.READY):
            lead.status = LeadStatus.READY
        self.session.flush()
        log_event("email_generated", lead_id=lead.id, message_id=message.id, kind="INITIAL",
                  status=status, flags=message.quality_flags, ai=ai_generated)
        return message

    def _generate_with_gate(self, prompt: str, facts: str, regenerate: bool
                            ) -> tuple[str, str, str, QualityReport, bool]:
        """Call the AI (max 2 attempts) and apply the quality gate."""
        attempt_prompt = prompt
        for attempt in range(2):
            try:
                data = self.ai.generate_json(
                    "cold_email", attempt_prompt, use_cache=not regenerate and attempt == 0,
                    temperature=0.8 if regenerate else None, max_tokens=450,
                )
            except AIError as exc:
                logger.warning("Email generation AI error: %s", exc)
                break
            subject = " ".join(str(data.get("subject") or "").split())
            body = clean_ai_body(str(data.get("body") or ""))
            reason = str(data.get("personalization_reason") or "")
            quality = check_quality(subject, body, facts)
            if not quality.serious:
                return subject, body, reason, quality, True
            attempt_prompt = (prompt + f"\nYour previous draft broke these rules: "
                              f"{', '.join(quality.flags)}. Fix them.")
        return "", "", "", QualityReport(), False

    @staticmethod
    def _template_initial(lead: Lead, campaign: Campaign) -> tuple[str, str, str]:
        research = lead.research
        if research.get("relevant_signal") and research.get("confidence", 0) >= 0.4:
            opener = f"I was looking at {lead.company_name} and noticed: {research['relevant_signal'].rstrip('.')}."
            reason = "Template: used website signal from research."
        else:
            role = f" as {lead.contact_role}" if lead.contact_role else ""
            opener = f"I'm reaching out to you{role} at {lead.company_name}."
            reason = "Template: light personalisation (company and role only)."
        body = (f"{opener}\n\n{campaign.product_name}: {campaign.value_proposition.rstrip('.')}.\n\n"
                f"{campaign.call_to_action}")
        return f"{campaign.product_name} for {lead.company_name}"[:80], body, reason

    # ------------------------------------------------------------------ follow-ups
    def generate_followup(self, lead: Lead, previous: EmailMessage) -> EmailMessage | None:
        campaign = lead.campaign
        number = lead.followup_count + 1
        max_followups = min(campaign.max_followups, self.settings.max_followups)
        facts = " ".join([campaign.product_name, campaign.value_proposition, lead.company_name])
        body, reason, ai_generated = "", "", False
        try:
            data = self.ai.generate_json("followup", followup_prompt(
                followup_number=number, max_followups=max_followups, product=campaign.product_name,
                value_prop=campaign.value_proposition, cta=campaign.call_to_action,
                recipient_name=first_name(lead.contact_name), recipient_company=lead.company_name,
                previous_email=previous.body), max_tokens=250)
            body = clean_ai_body(str(data.get("body") or ""))
            reason = str(data.get("personalization_reason") or "")
            ai_generated = True
        except AIError as exc:
            logger.warning("Follow-up AI error, using template: %s", exc)
        quality = check_quality(None, body, facts, min_words=15, max_words=110)
        if not ai_generated or quality.serious or _too_similar(body, previous.body):
            body = self._template_followup(lead, number, max_followups)
            reason = "Template follow-up"
            quality = QualityReport(["template_fallback"])
            ai_generated = False

        status, approved_by = approval_state(self.settings, campaign, MessageKind.FOLLOWUP, quality)
        message = EmailMessage(
            lead_id=lead.id, campaign_id=campaign.id, kind=MessageKind.FOLLOWUP, sequence=number,
            to_email=lead.email, subject=reply_subject(previous.subject),
            body=compose_email(body, lead.contact_name, self.settings, campaign, include_opt_out=False),
            personalization_reason=reason, quality_flags=",".join(quality.flags),
            ai_generated=ai_generated, status=status, approved_by=approved_by,
            approved_at=utcnow() if approved_by else None,
            gmail_thread_id=lead.gmail_thread_id, in_reply_to=previous.rfc_message_id,
        )
        self.session.add(message)
        self.session.flush()
        log_event("followup_generated", lead_id=lead.id, message_id=message.id, number=number, status=status)
        return message

    @staticmethod
    def _template_followup(lead: Lead, number: int, max_followups: int) -> str:
        company = lead.company_name
        if number >= max_followups:
            return (f"I'll leave it here and won't follow up again. If this becomes relevant for "
                    f"{company} later, just reply to this email and I'll send a short overview.")
        return (f"Just following up in case my previous email got buried. Would this be worth "
                f"exploring for {company}? Happy to send a short example instead of a call.")

    # ------------------------------------------------------------------ reply drafts
    def generate_reply_draft(self, lead: Lead, reply: Reply, category: str,
                             kind: str = MessageKind.REPLY) -> EmailMessage | None:
        """Draft a response to a lead's reply. ALWAYS requires human approval,
        except a referral request, which may auto-send in AUTOMATIC mode."""
        campaign = lead.campaign
        decision = check_can_contact(self.session, lead, campaign, kind)
        if not decision.allowed:
            return None
        try:
            data = self.ai.generate_json("reply_draft", reply_draft_prompt(
                category=category, product=campaign.product_name, value_prop=campaign.value_proposition,
                cta=campaign.call_to_action, recipient_name=first_name(lead.contact_name),
                recipient_company=lead.company_name, their_message=reply.body), max_tokens=350)
            body = clean_ai_body(str(data.get("body") or ""))
            ai_generated = True
        except AIError as exc:
            logger.warning("Reply draft AI error: %s", exc)
            body, ai_generated = "", False
        if not body:
            body = ("Thanks for your reply. I'll get back to you shortly with the details."
                    if kind == MessageKind.REPLY else
                    "Thanks for letting me know. Could you point me to the right person for this?")
        quality = check_quality(None, body, campaign.value_proposition, min_words=10, max_words=160)
        auto = (kind == MessageKind.REFERRAL_REQUEST and not self.settings.safe_mode
                and campaign.auto_send and not quality.serious)
        message = EmailMessage(
            lead_id=lead.id, campaign_id=campaign.id, kind=kind, sequence=0,
            to_email=reply.from_email or lead.email, subject=reply_subject(reply.subject),
            body=compose_email(body, lead.contact_name, self.settings, campaign, include_opt_out=False),
            personalization_reason=f"Suggested response to a {category} reply",
            quality_flags=",".join(quality.flags), ai_generated=ai_generated,
            status=MessageStatus.APPROVED if auto else MessageStatus.PENDING_APPROVAL,
            approved_by="auto" if auto else None, approved_at=utcnow() if auto else None,
            gmail_thread_id=reply.gmail_thread_id, in_reply_to=reply.rfc_message_id, reply_id=reply.id,
        )
        self.session.add(message)
        self.session.flush()
        log_event("email_generated", lead_id=lead.id, message_id=message.id, kind=kind, category=category)
        return message


def _too_similar(new: str, old: str, threshold: float = 0.6) -> bool:
    """Crude word-overlap check so follow-ups never repeat the first email."""
    new_words = set(re.findall(r"\w{4,}", new.lower()))
    old_words = set(re.findall(r"\w{4,}", old.lower()))
    if not new_words:
        return True
    return len(new_words & old_words) / len(new_words) > threshold
