"""Human-in-the-loop approval queue (SAFE MODE)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config.logging_config import log_event
from app.database.models import EmailMessage, LeadStatus, MessageKind, MessageStatus
from app.outreach.compliance import check_can_contact
from app.outreach.email_generator import EmailGenerator
from app.outreach.status_machine import can_transition_lead
from app.utils.helpers import utcnow
from app.utils.validators import sanitize_single_line, sanitize_text


class ApprovalError(ValueError):
    pass


class ApprovalService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def _require_pending(self, message: EmailMessage) -> None:
        if message.status != MessageStatus.PENDING_APPROVAL:
            raise ApprovalError(f"Message {message.id} is {message.status}, not pending approval")

    def edit(self, message: EmailMessage, subject: str, body: str) -> EmailMessage:
        self._require_pending(message)
        subject = sanitize_single_line(subject, 200)
        body = sanitize_text(body, 5000)
        if not subject or not body:
            raise ApprovalError("Subject and body must not be empty")
        message.subject, message.body = subject, body
        if "edited" not in message.quality_flags:
            message.quality_flags = ",".join(f for f in [message.quality_flags, "edited"] if f)
        self.session.commit()
        return message

    def approve(self, message: EmailMessage, subject: str | None = None, body: str | None = None
                ) -> EmailMessage:
        self._require_pending(message)
        if subject is not None and body is not None and (subject != message.subject or body != message.body):
            self.edit(message, subject, body)
        lead = message.lead
        decision = check_can_contact(self.session, lead, lead.campaign, message.kind)
        if not decision.allowed:
            raise ApprovalError(f"Cannot approve: {decision.reason}")
        message.status = MessageStatus.APPROVED
        message.approved_by = "human"
        message.approved_at = utcnow()
        self.session.commit()
        log_event("email_approved", message_id=message.id, lead_id=lead.id, kind=message.kind)
        return message

    def reject(self, message: EmailMessage, reason: str = "rejected by reviewer") -> EmailMessage:
        self._require_pending(message)
        message.status = MessageStatus.REJECTED
        message.error = sanitize_single_line(reason, 255)
        lead = message.lead
        if message.kind == MessageKind.INITIAL and lead.status == LeadStatus.READY:
            back = LeadStatus.RESEARCHED if lead.researched_at else LeadStatus.NEW
            if can_transition_lead(lead.status, back):
                lead.status = back
        self.session.commit()
        log_event("email_rejected", message_id=message.id, lead_id=lead.id)
        return message

    def regenerate(self, message: EmailMessage, generator: EmailGenerator) -> EmailMessage | None:
        """Replace a pending initial draft with a fresh AI version (bypasses the cache)."""
        self._require_pending(message)
        if message.kind != MessageKind.INITIAL:
            raise ApprovalError("Only initial emails can be regenerated; edit replies/follow-ups instead")
        new = generator.generate_initial(message.lead, regenerate=True)
        self.session.commit()
        return new
