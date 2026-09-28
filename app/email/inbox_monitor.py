"""Periodic reply detection.

Cost-conscious flow::

    tracked threads?  NO -> return (not even a Gmail call)
    list recent inbound ids (1 cheap call, ids only)
    any id on a tracked thread that we haven't processed?  NO -> return (zero AI)
    YES -> fetch just those messages, classify, apply rules

Every handled Gmail message id is stored in ``processed_messages`` in the same
transaction as its ``Reply`` row, so nothing is ever processed twice.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.config.logging_config import get_logger, log_event
from app.config.settings import Settings
from app.database.repositories import (
    AppSettingRepository, LeadRepository, MessageRepository, ProcessedMessageRepository,
)
from app.email.gmail_client import GmailAuthError, GmailClient, GmailError
from app.outreach.reply_handler import ReplyProcessor
from app.utils.helpers import utcnow

logger = get_logger("inbox")


@dataclass
class InboxCheckResult:
    tracked_threads: int = 0
    listed: int = 0
    new_replies: int = 0
    errors: int = 0
    categories: dict[str, int] = field(default_factory=dict)
    note: str = ""

    def __str__(self) -> str:
        cats = ", ".join(f"{k}={v}" for k, v in self.categories.items())
        return (f"tracked={self.tracked_threads} listed={self.listed} new={self.new_replies} "
                f"errors={self.errors}{' [' + cats + ']' if cats else ''}{' - ' + self.note if self.note else ''}")


class InboxMonitor:
    def __init__(self, session: Session, settings: Settings, gmail: GmailClient,
                 processor: ReplyProcessor) -> None:
        self.session = session
        self.settings = settings
        self.gmail = gmail
        self.processor = processor
        self.leads = LeadRepository(session)
        self.messages = MessageRepository(session)
        self.processed = ProcessedMessageRepository(session)

    def check(self) -> InboxCheckResult:
        result = InboxCheckResult()
        tracked = self.leads.tracked_thread_ids()
        result.tracked_threads = len(tracked)
        if not tracked:
            result.note = "no tracked threads - nothing to check"
            return result

        try:
            refs = self.gmail.list_recent_inbound(self.settings.inbox_lookback_days)
        except GmailAuthError:
            raise
        except GmailError as exc:
            log_event("error", f"Inbox check failed: {exc}", level=40, component="inbox")
            result.errors += 1
            result.note = str(exc)
            return result
        result.listed = len(refs)

        candidates = [r for r in refs if r.thread_id in tracked]
        done = self.processed.processed_ids([r.id for r in candidates])
        new = [r for r in candidates if r.id not in done and not self.messages.is_own_message(r.id)]
        if not new:
            self._touch()
            result.note = "no new replies - no AI used"
            return result

        own_address = ""
        try:
            own_address = self.gmail.profile_email().lower()
        except GmailError:
            pass  # only used to skip our own messages; ids of sent mail are checked too

        for ref in new:
            try:
                message = self.gmail.get_message(ref.id)
            except GmailAuthError:
                raise
            except (GmailError, KeyError) as exc:
                logger.warning("Could not fetch message %s: %s", ref.id, exc)
                result.errors += 1
                continue
            lead = self.leads.get_by_thread(ref.thread_id)
            if lead is None:
                continue
            if own_address and message.from_email == own_address:
                self.processed.mark(ref.id, ref.thread_id, "own_message")
                self.session.commit()
                continue
            try:
                with self.session.begin_nested():
                    reply = self.processor.handle(lead, message)
                    self.processed.mark(ref.id, ref.thread_id, str(reply.category))
                self.session.commit()
                result.new_replies += 1
                result.categories[str(reply.category)] = result.categories.get(str(reply.category), 0) + 1
            except Exception as exc:  # one bad message must not stop the others
                self.session.rollback()
                result.errors += 1
                log_event("error", f"Failed to process message {ref.id}: {type(exc).__name__}: {exc}",
                          level=40, component="inbox", lead_id=lead.id)
                # Mark as processed so a poison message cannot burn AI quota on every run.
                self.processed.mark(ref.id, ref.thread_id, "error")
                self.session.commit()
        self._touch()
        return result

    def _touch(self) -> None:
        AppSettingRepository(self.session).set(AppSettingRepository.LAST_INBOX_CHECK, utcnow().isoformat())
        self.session.commit()
