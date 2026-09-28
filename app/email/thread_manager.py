"""Thread helpers: quoted-text stripping, header chains and deterministic
bounce / auto-reply detection (these never need an LLM)."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from dateutil import parser as date_parser

from app.database.models import EmailMessage, Lead, MessageStatus
from app.email.gmail_client import InboxMessage
from app.utils.helpers import truncate

_QUOTE_MARKERS = [
    re.compile(r"^On .{0,200}wrote:\s*$", re.I),
    re.compile(r"^Am .{0,200}schrieb .{0,100}:\s*$", re.I),
    re.compile(r"^Le .{0,200}a écrit\s*:\s*$", re.I),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.I),
    re.compile(r"^-{2,}\s*Ursprüngliche Nachricht\s*-{2,}", re.I),
    re.compile(r"^_{10,}\s*$"),
    re.compile(r"^From:\s.+", re.I),
    re.compile(r"^Von:\s.+", re.I),
]

BOUNCE_SENDERS = ("mailer-daemon", "postmaster", "mail-daemon")
BOUNCE_SUBJECTS = ("delivery status notification", "undeliverable", "undelivered mail",
                   "delivery failure", "returned mail", "failure notice", "unzustellbar")
AUTO_REPLY_SUBJECTS = ("out of office", "automatic reply", "auto reply", "autoreply", "auto-reply",
                       "abwesenheit", "automatische antwort", "away from the office", "on vacation")


def strip_quoted_text(body: str, max_chars: int = 1500) -> str:
    """Keep only the newest part of a reply (drop quoted history and signatures-ish tails)."""
    lines: list[str] = []
    for line in (body or "").replace("\r\n", "\n").split("\n"):
        stripped = line.strip()
        if stripped.startswith(">"):
            continue  # inline-quoted history
        if any(p.match(stripped) for p in _QUOTE_MARKERS):
            break
        lines.append(line.rstrip())
    text = "\n".join(lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return truncate(text, max_chars)


def is_bounce(msg: InboxMessage) -> bool:
    sender = msg.from_email.lower()
    subject = msg.subject.lower()
    return any(s in sender for s in BOUNCE_SENDERS) or any(s in subject for s in BOUNCE_SUBJECTS)


def is_auto_reply(msg: InboxMessage) -> bool:
    headers = msg.headers
    auto_submitted = headers.get("auto-submitted", "no").lower()
    if auto_submitted and auto_submitted != "no":
        return True
    if any(h in headers for h in ("x-autoreply", "x-autorespond")):
        return True
    return any(s in msg.subject.lower() for s in AUTO_REPLY_SUBJECTS)


_DATE_HINT = re.compile(
    r"(?:until|till|back on|returning on|return on|bis zum|bis|back|from)\s+"
    r"((?:\w+,?\s+)?\d{1,2}(?:st|nd|rd|th)?[.\s/-]+(?:\w+|\d{1,2})[.\s/-]*(?:\d{2,4})?"
    r"|\d{4}-\d{2}-\d{2}"
    r"|(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?,?(?:\s+\d{4})?)",
    re.I,
)


def extract_return_date(text: str, today: date) -> date | None:
    """Best-effort return date from an out-of-office text (pure Python)."""
    for match in _DATE_HINT.finditer(text or ""):
        candidate = match.group(1).strip(" .,")
        try:
            parsed = date_parser.parse(candidate, dayfirst=True, fuzzy=True,
                                       default=datetime(today.year, today.month, today.day)).date()
        except (ValueError, OverflowError):
            continue
        if parsed < today and (today - parsed).days > 30:
            # "until 3 January" written in December refers to next year
            try:
                parsed = parsed.replace(year=parsed.year + 1)
            except ValueError:  # 29 February
                continue
        if today <= parsed <= today + timedelta(days=365):
            return parsed
    return None


def references_chain(lead: Lead) -> str:
    """RFC 5322 References header: all known Message-IDs in the conversation."""
    ids: list[str] = []
    for msg in lead.messages:
        if msg.status == MessageStatus.SENT and msg.rfc_message_id:
            ids.append(msg.rfc_message_id)
    for reply in lead.replies:
        if reply.rfc_message_id:
            ids.append(reply.rfc_message_id)
    seen: set[str] = set()
    ordered = [i for i in ids if not (i in seen or seen.add(i))]
    return " ".join(ordered[-10:])


def reply_threading(lead: Lead, message: EmailMessage) -> tuple[str | None, str | None, str | None]:
    """Return (thread_id, in_reply_to, references) for an outbound message."""
    thread_id = message.gmail_thread_id or lead.gmail_thread_id
    if not thread_id:
        return None, None, None
    in_reply_to = message.in_reply_to
    if not in_reply_to:
        sent = [m for m in lead.messages if m.status == MessageStatus.SENT and m.rfc_message_id]
        in_reply_to = sent[-1].rfc_message_id if sent else None
    return thread_id, in_reply_to, references_chain(lead) or in_reply_to
