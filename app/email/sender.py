"""Sends approved emails with hard safety limits.

Safety layers applied to *every* message, in order:

1. global kill switch (``sending_paused`` app setting)
2. global daily limit (``MAX_EMAILS_PER_DAY``) and per-campaign daily limit
3. pacing (``MIN_SECONDS_BETWEEN_EMAILS``)
4. SAFE MODE: only messages approved by a human may leave
5. reserved demo domains (example.com, *.test, ...) are never sent for real
6. compliance check (suppression list, do-not-contact, status, campaign state)
7. atomic claim APPROVED -> SENDING so two workers can never double-send
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.config.logging_config import get_logger, log_event
from app.config.settings import Settings
from app.database.models import EmailMessage, LeadStatus, MessageKind, MessageStatus
from app.database.repositories import AppSettingRepository, MessageRepository, start_of_utc_day
from app.email.gmail_client import GmailAuthError, GmailClient, GmailError
from app.email.thread_manager import reply_threading
from app.outreach.compliance import check_can_contact
from app.outreach.followup_service import schedule_next_followup
from app.outreach.status_machine import can_transition_lead
from app.utils.helpers import utcnow
from app.utils.validators import is_reserved_domain

logger = get_logger("sender")


@dataclass
class SendResult:
    sent: int = 0
    failed: int = 0
    skipped: int = 0
    stopped_reason: str | None = None

    def __str__(self) -> str:
        tail = f" (stopped: {self.stopped_reason})" if self.stopped_reason else ""
        return f"sent={self.sent} failed={self.failed} skipped={self.skipped}{tail}"


class SendService:
    def __init__(self, session: Session, settings: Settings, gmail: GmailClient,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = utcnow) -> None:
        self.session = session
        self.settings = settings
        self.gmail = gmail
        self.sleep = sleep
        self.clock = clock
        self.messages = MessageRepository(session)
        self.app_settings = AppSettingRepository(session)

    # ------------------------------------------------------------------ limits
    def remaining_today(self) -> int:
        return max(0, self.settings.max_emails_per_day - self.messages.sent_today(self.clock()))

    def seconds_until_next_send(self) -> float:
        last = self.messages.last_sent_at()
        if last is None:
            return 0.0
        return max(0.0, self.settings.min_seconds_between_emails - (self.clock() - last).total_seconds())

    def _log_limit_once(self) -> None:
        today = start_of_utc_day(self.clock()).date().isoformat()
        if self.app_settings.get(AppSettingRepository.LAST_LIMIT_HIT) != today:
            self.app_settings.set(AppSettingRepository.LAST_LIMIT_HIT, today)
            log_event("send_limit_reached", f"Daily limit of {self.settings.max_emails_per_day} reached; "
                      "sending resumes tomorrow (UTC).", level=30, limit=self.settings.max_emails_per_day)

    # ------------------------------------------------------------------ main loop
    def process_queue(self, max_messages: int | None = None, wait: bool = False) -> SendResult:
        """Send approved messages until the queue is empty or a limit stops us.

        ``wait=False`` (dashboard): never block; stop when pacing requires waiting.
        ``wait=True`` (scheduled job): sleep between messages to respect pacing.
        """
        result = SendResult()
        if self.app_settings.get_bool(AppSettingRepository.SENDING_PAUSED):
            result.stopped_reason = "sending is paused (kill switch)"
            return result

        for message in self.messages.ready_to_send(self.clock()):
            if max_messages is not None and result.sent >= max_messages:
                break
            if self.remaining_today() <= 0:
                self._log_limit_once()
                result.stopped_reason = "daily limit reached"
                break
            campaign = message.lead.campaign
            if self.messages.sent_today(self.clock(), campaign.id) >= campaign.max_emails_per_day:
                result.skipped += 1
                continue
            wait_s = self.seconds_until_next_send()
            if wait_s > 0:
                if not wait:
                    result.stopped_reason = f"pacing: next send allowed in {int(wait_s)}s"
                    break
                self.sleep(wait_s)

            outcome = self._send_one(message)
            if outcome == "sent":
                result.sent += 1
            elif outcome == "failed":
                result.failed += 1
            elif outcome == "auth":
                result.failed += 1
                result.stopped_reason = "Gmail authorisation problem - run: python run.py gmail-auth"
                break
            else:
                result.skipped += 1
        self.session.commit()
        return result

    def _send_one(self, message: EmailMessage) -> str:
        lead = message.lead
        campaign = lead.campaign

        if self.settings.safe_mode and message.approved_by != "human":
            # Auto-approved while automatic mode was on, but SAFE MODE is on now.
            message.status = MessageStatus.PENDING_APPROVAL
            message.approved_by = None
            self.session.commit()
            return "skipped"
        if not self.gmail.is_demo and is_reserved_domain(message.to_email):
            message.status = MessageStatus.FAILED
            message.error = "reserved/demo domain - never sent for real"
            self.session.commit()
            return "failed"
        decision = check_can_contact(self.session, lead, campaign, message.kind)
        if not decision.allowed:
            message.status = MessageStatus.CANCELLED
            message.error = f"blocked before sending: {decision.reason}"
            self.session.commit()
            logger.info("Message %s cancelled: %s", message.id, decision.reason)
            return "skipped"
        if not self.messages.claim_for_sending(message.id):
            return "skipped"  # another worker got it
        self.session.commit()

        thread_id, in_reply_to, references = (None, None, None)
        if message.kind != MessageKind.INITIAL:
            self.session.refresh(lead, ["messages", "replies"])  # other processes may have added rows
            thread_id, in_reply_to, references = reply_threading(lead, message)
        try:
            sent = self.gmail.send(message.to_email, message.subject, message.body,
                                   from_name=campaign.sender_name or self.settings.sender_name,
                                   thread_id=thread_id, in_reply_to=in_reply_to, references=references)
        except GmailAuthError as exc:
            message.status = MessageStatus.APPROVED  # retry after re-authorisation
            self.session.commit()
            log_event("error", str(exc), level=40, component="gmail_auth")
            return "auth"
        except (GmailError, OSError) as exc:
            message.status = MessageStatus.FAILED
            message.error = str(exc)[:500]
            self.session.commit()
            log_event("email_send_failed", message_id=message.id, lead_id=lead.id, error=str(exc)[:200], level=40)
            return "failed"

        now = self.clock()
        message.status = MessageStatus.SENT
        message.sent_at = now
        message.gmail_message_id = sent.gmail_message_id
        message.gmail_thread_id = sent.thread_id
        message.rfc_message_id = sent.rfc_message_id
        message.error = None
        lead.last_contacted_at = now
        if not lead.gmail_thread_id:
            lead.gmail_thread_id = sent.thread_id

        if message.kind in (MessageKind.INITIAL, MessageKind.FOLLOWUP):
            if message.kind == MessageKind.FOLLOWUP:
                lead.followup_count += 1
            if can_transition_lead(lead.status, LeadStatus.EMAIL_SENT):
                lead.status = LeadStatus.EMAIL_SENT
            schedule_next_followup(lead, now, self.settings)
            if lead.next_followup_at is None:
                # Last follow-up sent: wait one more interval, then close the lead.
                lead.next_followup_at = now + timedelta(days=campaign.followup_2_days)
        self.session.commit()
        log_event("email_sent", message_id=message.id, lead_id=lead.id, kind=message.kind,
                  demo=self.gmail.is_demo, thread_id=sent.thread_id)
        return "sent"
