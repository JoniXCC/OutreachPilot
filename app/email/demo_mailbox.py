"""A local, file-backed fake Gmail used in DEMO_MODE and tests.

Outbound emails are written to a JSON file instead of being sent. Replies can
be simulated so the full pipeline (inbox monitor -> classifier -> rules ->
follow-ups) can be demonstrated without emailing anyone.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timedelta
from email.utils import make_msgid
from pathlib import Path
from typing import Any

from app.email.gmail_client import GmailClient, InboxMessage, MessageRef, SentMessage
from app.utils.helpers import utcnow

DEMO_ADDRESS = "demo-sender@outreach.local"

# Canned replies for the "simulate reply" button in the dashboard.
SCENARIOS: dict[str, dict[str, Any]] = {
    "interested": {"body": "Hi, thanks for reaching out - this sounds interesting. Could we set up a "
                           "call next Tuesday? Also, what does it cost for a clinic our size?"},
    "question": {"body": "Does this integrate with our existing booking software, and is patient "
                         "data stored in the EU?"},
    "more_info": {"body": "Can you send me more information first? A short overview would help."},
    "not_interested": {"body": "Thanks, but we're not interested at the moment."},
    "unsubscribe": {"body": "Please remove me from your list and do not contact me again."},
    "wrong_person": {"body": "I'm not the right person for this - I only handle accounting here."},
    "out_of_office": {"body": "I am out of office until {return_date} with limited access to email. "
                              "I will respond when I am back.",
                      "headers": {"auto-submitted": "auto-replied"},
                      "subject_prefix": "Automatic reply: "},
    "bounce": {"body": "Address not found. Your message wasn't delivered because the address "
                       "couldn't be found or is unable to receive mail.",
               "from": "mailer-daemon@googlemail.com", "subject_prefix": "Delivery Status Notification (Failure) "},
    "unclear": {"body": "Hmm."},
}


class DemoMailbox(GmailClient):
    is_demo = True
    _lock = threading.Lock()

    def __init__(self, path: Path) -> None:
        self.path = path

    # ------------------------------------------------------------------ storage
    def _load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            return json.loads(self.path.read_text(encoding="utf-8")).get("messages", [])
        except (json.JSONDecodeError, OSError):
            return []

    def _save(self, messages: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"messages": messages}, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.path)

    def _append(self, record: dict[str, Any]) -> None:
        with DemoMailbox._lock:
            messages = self._load()
            messages.append(record)
            self._save(messages)

    @staticmethod
    def _to_inbox(r: dict[str, Any]) -> InboxMessage:
        return InboxMessage(
            id=r["id"], thread_id=r["thread_id"], from_email=r["from"], to=r["to"],
            subject=r["subject"], date=datetime.fromisoformat(r["date"]), body_text=r["body"],
            rfc_message_id=r["rfc_message_id"], in_reply_to=r.get("in_reply_to") or "",
            headers=r.get("headers") or {},
        )

    # ------------------------------------------------------------------ GmailClient API
    def profile_email(self) -> str:
        return DEMO_ADDRESS

    def send(self, to: str, subject: str, body: str, *, from_name: str | None = None,
             thread_id: str | None = None, in_reply_to: str | None = None,
             references: str | None = None) -> SentMessage:
        msg_id = f"demo-{uuid.uuid4().hex[:12]}"
        thread = thread_id or f"demo-thread-{uuid.uuid4().hex[:10]}"
        rfc = make_msgid(domain="outreach.local")
        self._append({"id": msg_id, "thread_id": thread, "direction": "out", "from": DEMO_ADDRESS,
                      "to": to, "subject": subject, "body": body, "date": utcnow().isoformat(),
                      "rfc_message_id": rfc, "in_reply_to": in_reply_to, "headers": {}})
        return SentMessage(msg_id, thread, rfc)

    def list_recent_inbound(self, days: int) -> list[MessageRef]:
        cutoff = utcnow() - timedelta(days=days)
        return [MessageRef(r["id"], r["thread_id"]) for r in self._load()
                if r["direction"] == "in" and datetime.fromisoformat(r["date"]) >= cutoff]

    def get_message(self, message_id: str) -> InboxMessage:
        for r in self._load():
            if r["id"] == message_id:
                return self._to_inbox(r)
        raise KeyError(message_id)

    def get_thread(self, thread_id: str) -> list[InboxMessage]:
        return [self._to_inbox(r) for r in self._load() if r["thread_id"] == thread_id]

    # ------------------------------------------------------------------ demo helpers
    def outbound(self) -> list[dict[str, Any]]:
        return [r for r in self._load() if r["direction"] == "out"]

    def inject_reply(self, thread_id: str, from_email: str, body: str, subject: str = "",
                     headers: dict[str, str] | None = None) -> str:
        """Simulate an incoming email on a thread. Returns the new message id."""
        thread = self.get_thread(thread_id)
        last = thread[-1] if thread else None
        msg_id = f"demo-in-{uuid.uuid4().hex[:12]}"
        self._append({
            "id": msg_id, "thread_id": thread_id, "direction": "in", "from": from_email.lower(),
            "to": DEMO_ADDRESS, "subject": subject or (f"Re: {last.subject}" if last else "Re:"),
            "body": body, "date": utcnow().isoformat(),
            "rfc_message_id": make_msgid(domain="lead.example"),
            "in_reply_to": last.rfc_message_id if last else "", "headers": headers or {},
        })
        return msg_id

    def simulate_scenario(self, thread_id: str, lead_email: str, scenario: str) -> str:
        spec = SCENARIOS[scenario]
        thread = self.get_thread(thread_id)
        base_subject = thread[0].subject if thread else ""
        return_date = (utcnow() + timedelta(days=6)).date().isoformat()
        body = spec["body"].format(return_date=return_date)
        subject = spec.get("subject_prefix", "Re: ") + base_subject
        quoted = "\n\nOn Mon, someone wrote:\n> " + "\n> ".join((thread[-1].body_text if thread else "").splitlines()[:5])
        return self.inject_reply(thread_id, spec.get("from", lead_email), body + quoted, subject,
                                 dict(spec.get("headers", {})))
