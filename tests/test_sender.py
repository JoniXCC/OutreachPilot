"""Sending pipeline tests: daily limits, pacing, safe mode, suppression, Gmail errors."""

import base64
from datetime import timedelta

import pytest

from app.database.models import EmailMessage, LeadStatus, MessageKind, MessageStatus
from app.database.repositories import AppSettingRepository, SuppressionRepository
from app.email.demo_mailbox import DemoMailbox
from app.email.gmail_client import (
    GmailAuthError, GmailClient, GmailError, SentMessage, build_mime, parse_gmail_message,
)
from app.email.sender import SendService
from app.utils.helpers import utcnow


class FakeGmail(GmailClient):
    """Records sends in memory; can be told to fail."""

    is_demo = True

    def __init__(self, fail_with: Exception | None = None):
        self.sent: list[dict] = []
        self.fail_with = fail_with

    def send(self, to, subject, body, **kw):
        if self.fail_with:
            raise self.fail_with
        n = len(self.sent) + 1
        self.sent.append({"to": to, "subject": subject, **kw})
        return SentMessage(f"m{n}", kw.get("thread_id") or f"t{n}", f"<m{n}@x>")

    def list_recent_inbound(self, days): return []
    def get_message(self, message_id): raise KeyError(message_id)
    def get_thread(self, thread_id): return []
    def profile_email(self): return "me@x.com"


def approved(session, lead, **kw):
    msg = EmailMessage(lead_id=lead.id, campaign_id=lead.campaign_id, to_email=lead.email,
                       subject=kw.pop("subject", "Hello"), body="Body", status=MessageStatus.APPROVED,
                       approved_by=kw.pop("approved_by", "human"), kind=kw.pop("kind", MessageKind.INITIAL), **kw)
    session.add(msg)
    lead.status = kw.get("lead_status", LeadStatus.READY)
    session.commit()
    return msg


def many_leads(session, lead_factory, n):
    return [lead_factory(f"person{i}@company{i}.de", company_name=f"Company {i}", contact_name=f"P {i}")
            for i in range(n)]


def test_sends_and_updates_lead(session, settings, lead_factory):
    lead = lead_factory()
    msg = approved(session, lead)
    gmail = FakeGmail()
    result = SendService(session, settings, gmail).process_queue()
    assert result.sent == 1
    assert msg.status == MessageStatus.SENT and msg.gmail_thread_id == "t1"
    assert lead.status == LeadStatus.EMAIL_SENT
    assert lead.gmail_thread_id == "t1"
    assert lead.next_followup_at is not None
    assert (lead.next_followup_at - msg.sent_at).days == settings.followup_1_days


def test_daily_limit_stops_sending(session, settings, lead_factory):
    leads = many_leads(session, lead_factory, 8)
    for lead in leads:
        approved(session, lead)
    gmail = FakeGmail()
    result = SendService(session, settings, gmail).process_queue()
    assert result.sent == settings.max_emails_per_day == 5
    assert result.stopped_reason == "daily limit reached"
    # a second run the same day sends nothing
    assert SendService(session, settings, gmail).process_queue().sent == 0
    assert AppSettingRepository(session).get("send_limit_reached_on")


def test_daily_limit_resets_next_day(session, settings, lead_factory):
    leads = many_leads(session, lead_factory, 7)
    for lead in leads:
        approved(session, lead)
    SendService(session, settings, FakeGmail()).process_queue()
    tomorrow = utcnow() + timedelta(days=1)
    result = SendService(session, settings, FakeGmail(), clock=lambda: tomorrow).process_queue()
    assert result.sent == 2


def test_campaign_daily_limit(session, settings, campaign, lead_factory):
    campaign.max_emails_per_day = 2
    for lead in many_leads(session, lead_factory, 4):
        approved(session, lead)
    result = SendService(session, settings, FakeGmail()).process_queue()
    assert result.sent == 2 and result.skipped == 2


def test_pacing_without_wait_stops(session, settings, lead_factory):
    paced = settings.model_copy(update={"min_seconds_between_emails": 90})
    for lead in many_leads(session, lead_factory, 3):
        approved(session, lead)
    result = SendService(session, paced, FakeGmail()).process_queue()
    assert result.sent == 1 and "pacing" in result.stopped_reason


def test_pacing_with_wait_sleeps(session, settings, lead_factory):
    paced = settings.model_copy(update={"min_seconds_between_emails": 90})
    for lead in many_leads(session, lead_factory, 3):
        approved(session, lead)
    sleeps: list[float] = []
    current = [utcnow()]

    def fake_sleep(seconds):
        sleeps.append(seconds)
        current[0] += timedelta(seconds=seconds)

    result = SendService(session, paced, FakeGmail(), sleep=fake_sleep, clock=lambda: current[0]).process_queue(wait=True)
    assert result.sent == 3
    assert len(sleeps) == 2 and all(85 <= s <= 90 for s in sleeps)


def test_safe_mode_blocks_auto_approved(session, settings, lead_factory):
    lead = lead_factory()
    msg = approved(session, lead, approved_by="auto")
    gmail = FakeGmail()
    result = SendService(session, settings, gmail).process_queue()
    assert result.sent == 0 and gmail.sent == []
    assert msg.status == MessageStatus.PENDING_APPROVAL


def test_automatic_mode_sends_auto_approved(session, settings, lead_factory):
    lead = lead_factory()
    approved(session, lead, approved_by="auto")
    unsafe = settings.model_copy(update={"safe_mode": False})
    assert SendService(session, unsafe, FakeGmail()).process_queue().sent == 1


def test_suppressed_address_never_sent(session, settings, lead_factory):
    lead = lead_factory()
    msg = approved(session, lead)
    SuppressionRepository(session).add_email(lead.email)
    gmail = FakeGmail()
    SendService(session, settings, gmail).process_queue()
    assert gmail.sent == [] and msg.status == MessageStatus.CANCELLED


def test_kill_switch(session, settings, lead_factory):
    approved(session, lead_factory())
    AppSettingRepository(session).set("sending_paused", "true")
    result = SendService(session, settings, FakeGmail()).process_queue()
    assert result.sent == 0 and "paused" in result.stopped_reason


def test_reserved_domain_never_sent_by_real_client(session, settings, lead_factory):
    lead = lead_factory("demo@example.com")
    msg = approved(session, lead)
    gmail = FakeGmail()
    gmail.is_demo = False
    SendService(session, settings, gmail).process_queue()
    assert gmail.sent == [] and msg.status == MessageStatus.FAILED


def test_gmail_error_marks_failed_and_continues(session, settings, lead_factory):
    leads = many_leads(session, lead_factory, 2)
    msgs = [approved(session, lead) for lead in leads]
    result = SendService(session, settings, FakeGmail(GmailError("boom"))).process_queue()
    assert result.failed == 2
    assert all(m.status == MessageStatus.FAILED for m in msgs)


def test_gmail_auth_error_keeps_message_for_retry(session, settings, lead_factory):
    msg = approved(session, lead_factory())
    result = SendService(session, settings, FakeGmail(GmailAuthError("expired"))).process_queue()
    assert "authorisation" in result.stopped_reason
    assert msg.status == MessageStatus.APPROVED


def test_followup_sent_in_same_thread(session, settings, lead_factory):
    lead = lead_factory()
    approved(session, lead)
    gmail = FakeGmail()
    SendService(session, settings, gmail).process_queue()
    follow = approved(session, lead, kind=MessageKind.FOLLOWUP, subject="Re: Hello", sequence=1)
    lead.status = LeadStatus.FOLLOWUP_DUE
    session.commit()
    SendService(session, settings, gmail).process_queue()
    assert gmail.sent[1]["thread_id"] == "t1"
    assert gmail.sent[1]["in_reply_to"] == "<m1@x>"
    assert follow.status == MessageStatus.SENT
    assert lead.followup_count == 1


# ------------------------------------------------------------------ MIME / parsing / demo mailbox
def test_build_mime_prevents_header_injection():
    mime = build_mime("a@b.com", "Hello\r\nBcc: victim@evil.com", "body", sender="me@x.com", from_name="Jon")
    assert "Bcc" not in mime.keys()
    assert mime["Subject"].startswith("Hello")


def test_parse_gmail_message_prefers_plain_text():
    def enc(s):
        return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")
    raw = {"id": "1", "threadId": "t", "payload": {
        "headers": [{"name": "From", "value": "Sarah <Sarah@ABC.de>"}, {"name": "Subject", "value": "Re: hi"},
                    {"name": "Date", "value": "Mon, 28 Sep 2026 10:00:00 +0200"},
                    {"name": "Message-ID", "value": "<abc@x>"}],
        "mimeType": "multipart/alternative",
        "parts": [{"mimeType": "text/plain", "body": {"data": enc("Yes please")}},
                  {"mimeType": "text/html", "body": {"data": enc("<p>Yes please</p>")}}]}}
    msg = parse_gmail_message(raw)
    assert msg.from_email == "sarah@abc.de" and msg.body_text == "Yes please"
    assert msg.date.hour == 8  # converted to UTC


def test_demo_mailbox_roundtrip(tmp_path):
    box = DemoMailbox(tmp_path / "box.json")
    sent = box.send("a@example.com", "Hello", "Body")
    reply_id = box.simulate_scenario(sent.thread_id, "a@example.com", "interested")
    refs = box.list_recent_inbound(7)
    assert [r.id for r in refs] == [reply_id]
    msg = box.get_message(reply_id)
    assert msg.thread_id == sent.thread_id and msg.in_reply_to == sent.rfc_message_id
    assert len(box.get_thread(sent.thread_id)) == 2
