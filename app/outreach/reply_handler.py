"""Turns a classified reply into deterministic actions.

| Category            | Action                                                          |
|---------------------|-----------------------------------------------------------------|
| BOUNCE              | suppress address, status BOUNCED                                |
| UNSUBSCRIBE         | do_not_contact + suppression list, cancel everything            |
| low confidence      | HUMAN_REVIEW, no automatic response, follow-ups paused          |
| NOT_INTERESTED      | status NOT_INTERESTED, cancel all follow-ups                    |
| OUT_OF_OFFICE       | follow-up the day after the return date, else HUMAN_REVIEW      |
| WRONG_PERSON        | one short "who is the right person?" draft - never repeated     |
| INTERESTED          | INTERESTED + human_review_required, suggested reply for a human |
| QUESTION / MORE_INFO| HUMAN_REVIEW with a draft reply awaiting approval               |
| UNCLEAR             | HUMAN_REVIEW                                                    |
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from app.config.logging_config import get_logger, log_event
from app.config.settings import Settings
from app.database.models import (
    Lead, LeadStatus, MessageKind, MessageStatus, Reply, ReplyCategory, SuppressionReason,
)
from app.database.repositories import MessageRepository
from app.email.gmail_client import InboxMessage
from app.email.thread_manager import strip_quoted_text
from app.outreach.compliance import suppress_lead
from app.outreach.email_generator import EmailGenerator
from app.outreach.followup_service import effective_max_followups
from app.outreach.reply_classifier import Classification, ReplyClassifier
from app.outreach.status_machine import can_transition_lead
from app.utils.helpers import utcnow

logger = get_logger("reply_handler")
C = ReplyCategory


class ReplyProcessor:
    def __init__(self, session: Session, settings: Settings, classifier: ReplyClassifier,
                 generator: EmailGenerator) -> None:
        self.session = session
        self.settings = settings
        self.classifier = classifier
        self.generator = generator
        self.messages = MessageRepository(session)

    def handle(self, lead: Lead, message: InboxMessage, today: date | None = None) -> Reply:
        today = today or utcnow().date()
        latest = strip_quoted_text(message.body_text)
        reply = Reply(
            lead_id=lead.id, campaign_id=lead.campaign_id, gmail_message_id=message.id,
            gmail_thread_id=message.thread_id, rfc_message_id=message.rfc_message_id or None,
            from_email=message.from_email, subject=message.subject[:255], body=latest,
            received_at=message.date,
        )
        lead.replies.append(reply)
        self.session.flush()
        log_event("reply_received", lead_id=lead.id, reply_id=reply.id)

        initial = self.messages.initial_for_lead(lead.id)
        classification = self.classifier.classify(message, latest, initial.subject if initial else "", today)
        reply.category = classification.category
        reply.confidence = classification.confidence
        reply.summary = classification.summary
        reply.requires_human = classification.requires_human
        reply.classified_by = classification.source
        reply.return_date = (datetime.combine(classification.return_date, time())
                             if classification.return_date else None)
        reply.action_taken = self.apply_rules(lead, reply, classification)
        self.session.flush()
        log_event("reply_classified", lead_id=lead.id, reply_id=reply.id, category=str(reply.category),
                  confidence=reply.confidence, source=reply.classified_by, action=reply.action_taken)
        return reply

    # ------------------------------------------------------------------ rules
    def apply_rules(self, lead: Lead, reply: Reply, c: Classification) -> str:
        lead.reply_status = c.category
        lead.next_followup_at = None
        # Any reply stops the automated sequence; a new plan is decided below.
        self.messages.cancel_open_for_lead(lead.id, [MessageKind.FOLLOWUP], "lead replied")
        self._move(lead, LeadStatus.REPLIED)

        if c.category == C.BOUNCE:
            suppress_lead(self.session, lead, SuppressionReason.BOUNCED, "hard bounce")
            return "bounced: address suppressed"

        if c.category == C.UNSUBSCRIBE and c.confidence >= self.settings.classification_confidence_threshold:
            suppress_lead(self.session, lead, SuppressionReason.UNSUBSCRIBE, "asked to stop")
            return "unsubscribed: permanently suppressed"

        if c.confidence < self.settings.classification_confidence_threshold:
            return self._human(lead, f"low confidence ({c.confidence:.2f}) - no automatic action")

        if c.category == C.NOT_INTERESTED:
            self.messages.cancel_open_for_lead(lead.id, reason="not interested")
            self._move(lead, LeadStatus.NOT_INTERESTED)
            return "not interested: outreach stopped"

        if c.category == C.OUT_OF_OFFICE:
            max_f = effective_max_followups(lead.campaign, self.settings)
            if c.return_date and lead.followup_count < max_f:
                self._move(lead, LeadStatus.OUT_OF_OFFICE)
                lead.next_followup_at = datetime.combine(c.return_date, time(8, 0)) + timedelta(days=1)
                log_event("followup_scheduled", lead_id=lead.id, due=lead.next_followup_at.isoformat(),
                          reason="out_of_office")
                return f"out of office until {c.return_date.isoformat()}: follow-up rescheduled"
            return self._human(lead, "out of office without a usable return date")

        if c.category == C.WRONG_PERSON:
            self._move(lead, LeadStatus.WRONG_PERSON)
            if c.referral_contact:
                lead.human_review_required = True
            already_asked = self.messages.has_kind(
                lead.id, MessageKind.REFERRAL_REQUEST,
                [MessageStatus.PENDING_APPROVAL, MessageStatus.APPROVED, MessageStatus.SENDING, MessageStatus.SENT])
            if already_asked:
                return "wrong person: referral already requested, no further contact"
            draft = self.generator.generate_reply_draft(lead, reply, C.WRONG_PERSON, MessageKind.REFERRAL_REQUEST)
            referral = f" (referral: {c.referral_contact})" if c.referral_contact else ""
            return f"wrong person: referral request drafted{referral}" if draft else "wrong person"

        if c.category == C.INTERESTED:
            self._move(lead, LeadStatus.INTERESTED)
            lead.human_review_required = True
            self.generator.generate_reply_draft(lead, reply, C.INTERESTED)
            log_event("human_review_requested", lead_id=lead.id, reason="interested")
            return "interested: escalated to human with suggested reply"

        if c.category in (C.QUESTION, C.MORE_INFORMATION):
            self.generator.generate_reply_draft(lead, reply, c.category)
            return self._human(lead, f"{c.category.lower()}: draft reply awaiting approval")

        return self._human(lead, "unclear reply")

    # ------------------------------------------------------------------ helpers
    def _move(self, lead: Lead, status: LeadStatus) -> None:
        if can_transition_lead(lead.status, status):
            lead.status = status
        else:
            # e.g. a NOT_INTERESTED lead writes again: keep status, let a human look.
            lead.human_review_required = True
            logger.info("Lead %s: %s -> %s not allowed, flagged for review", lead.id, lead.status, status)

    def _human(self, lead: Lead, reason: str) -> str:
        self._move(lead, LeadStatus.HUMAN_REVIEW)
        lead.human_review_required = True
        log_event("human_review_requested", lead_id=lead.id, reason=reason)
        return reason

