"""Gmail access through the official Gmail API (OAuth2, no browser scraping).

Scopes are deliberately minimal:
* ``gmail.send``     - send new emails and replies in existing threads
* ``gmail.readonly`` - read replies on threads we started

The OAuth token is stored locally in ``GMAIL_TOKEN_PATH`` (git-ignored) and
refreshed automatically.
"""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, make_msgid, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from app.config.logging_config import get_logger
from app.utils.helpers import utcnow
from app.utils.validators import sanitize_single_line

logger = get_logger("gmail")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailError(RuntimeError):
    """Any Gmail API failure."""


class GmailAuthError(GmailError):
    """Missing, expired or revoked OAuth credentials."""


@dataclass
class SentMessage:
    gmail_message_id: str
    thread_id: str
    rfc_message_id: str


@dataclass
class MessageRef:
    id: str
    thread_id: str


@dataclass
class InboxMessage:
    id: str
    thread_id: str
    from_email: str
    from_name: str = ""
    to: str = ""
    subject: str = ""
    date: datetime = field(default_factory=utcnow)
    body_text: str = ""
    rfc_message_id: str = ""
    in_reply_to: str = ""
    headers: dict[str, str] = field(default_factory=dict)  # lower-cased keys


def build_mime(to: str, subject: str, body: str, *, sender: str | None = None,
               from_name: str | None = None, in_reply_to: str | None = None,
               references: str | None = None) -> MimeMessage:
    """Plain-text MIME message. Header values are single-line sanitised (no header injection)."""
    msg = MimeMessage()
    msg["To"] = sanitize_single_line(to, 254)
    if sender:
        msg["From"] = formataddr((sanitize_single_line(from_name or "", 80), sender))
    msg["Subject"] = sanitize_single_line(subject, 200)
    domain = sender.rsplit("@", 1)[1] if sender and "@" in sender else "outreach.local"
    msg["Message-ID"] = make_msgid(domain=domain)
    if in_reply_to:
        msg["In-Reply-To"] = sanitize_single_line(in_reply_to, 255)
        msg["References"] = sanitize_single_line(references or in_reply_to, 2000)
    msg.set_content(body)
    return msg


class GmailClient(ABC):
    """Interface used by the sender and inbox monitor (real or demo)."""

    is_demo: bool = False

    @abstractmethod
    def send(self, to: str, subject: str, body: str, *, from_name: str | None = None,
             thread_id: str | None = None, in_reply_to: str | None = None,
             references: str | None = None) -> SentMessage: ...

    @abstractmethod
    def list_recent_inbound(self, days: int) -> list[MessageRef]:
        """Cheap listing (ids + thread ids only) of recent messages not sent by us."""

    @abstractmethod
    def get_message(self, message_id: str) -> InboxMessage: ...

    @abstractmethod
    def get_thread(self, thread_id: str) -> list[InboxMessage]: ...

    @abstractmethod
    def profile_email(self) -> str: ...


# --------------------------------------------------------------------------- parsing helpers
def _b64decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _charset(part: dict[str, Any]) -> str:
    for header in part.get("headers") or []:
        if header.get("name", "").lower() == "content-type" and "charset=" in header.get("value", ""):
            return header["value"].split("charset=")[-1].split(";")[0].strip("\"' ") or "utf-8"
    return "utf-8"


def extract_body(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to text extracted from text/html."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            try:
                text = _b64decode(data).decode(_charset(part), errors="replace")
            except (LookupError, ValueError):
                text = _b64decode(data).decode("utf-8", errors="replace")
            (plain if mime == "text/plain" else html).append(text)
        for sub in part.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip()
    if html:
        return BeautifulSoup("\n".join(html), "html.parser").get_text("\n").strip()
    return ""


def parse_gmail_message(raw: dict[str, Any]) -> InboxMessage:
    payload = raw.get("payload") or {}
    headers = {h["name"].lower(): h.get("value", "") for h in payload.get("headers") or []}
    from_name, from_email = parseaddr(headers.get("from", ""))
    try:
        date = parsedate_to_datetime(headers["date"]).astimezone(timezone.utc).replace(tzinfo=None)
    except (KeyError, TypeError, ValueError):
        internal = raw.get("internalDate")
        date = (datetime.fromtimestamp(int(internal) / 1000, tz=timezone.utc).replace(tzinfo=None)
                if internal else utcnow())
    return InboxMessage(
        id=raw["id"], thread_id=raw.get("threadId", ""), from_email=from_email.lower(),
        from_name=from_name, to=headers.get("to", ""), subject=headers.get("subject", ""),
        date=date, body_text=extract_body(payload), rfc_message_id=headers.get("message-id", ""),
        in_reply_to=headers.get("in-reply-to", ""), headers=headers,
    )


# --------------------------------------------------------------------------- real client
class GoogleGmailClient(GmailClient):
    def __init__(self, credentials_path: Path, token_path: Path, service: Any | None = None) -> None:
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = service
        self._email: str | None = None

    # ------------------------------------------------------------------ auth
    @staticmethod
    def authorize(credentials_path: Path, token_path: Path, interactive: bool = False) -> Any:
        """Return valid OAuth credentials, refreshing or (if interactive) running the consent flow."""
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            except (ValueError, OSError) as exc:
                logger.warning("Ignoring unreadable Gmail token file: %s", type(exc).__name__)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                GoogleGmailClient._save_token(creds, token_path)
                return creds
            except RefreshError as exc:
                if not interactive:
                    raise GmailAuthError(
                        "Gmail token expired or was revoked. Run: python run.py gmail-auth"
                    ) from exc
                creds = None
        if not interactive:
            raise GmailAuthError("Gmail is not authorised yet. Run: python run.py gmail-auth")
        if not credentials_path.exists():
            raise GmailAuthError(
                f"OAuth client file not found at {credentials_path}. Download it from Google Cloud "
                "Console (APIs & Services > Credentials > OAuth client ID > Desktop app)."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
        creds = flow.run_local_server(port=0, prompt="consent")
        GoogleGmailClient._save_token(creds, token_path)
        return creds

    @staticmethod
    def _save_token(creds: Any, token_path: Path) -> None:
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json(), encoding="utf-8")
        try:
            os.chmod(token_path, 0o600)  # owner-only on POSIX; harmless on Windows
        except OSError:
            pass

    @property
    def service(self) -> Any:
        if self._service is None:
            from googleapiclient.discovery import build
            creds = self.authorize(self.credentials_path, self.token_path, interactive=False)
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def _execute(self, request: Any) -> Any:
        from googleapiclient.errors import HttpError
        try:
            return request.execute(num_retries=2)
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status == 401:
                raise GmailAuthError("Gmail rejected the credentials (401). Run: python run.py gmail-auth") from exc
            if status == 429 or (status == 403 and "rateLimit" in str(exc)):
                raise GmailError("Gmail API rate limit reached - try again later") from exc
            raise GmailError(f"Gmail API error {status}: {exc.reason if hasattr(exc, 'reason') else exc}") from exc
        except (OSError, TimeoutError) as exc:
            raise GmailError(f"Network error talking to Gmail: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------ API
    def profile_email(self) -> str:
        if self._email is None:
            profile = self._execute(self.service.users().getProfile(userId="me"))
            self._email = profile.get("emailAddress", "")
        return self._email

    def send(self, to: str, subject: str, body: str, *, from_name: str | None = None,
             thread_id: str | None = None, in_reply_to: str | None = None,
             references: str | None = None) -> SentMessage:
        mime = build_mime(to, subject, body, sender=self.profile_email(), from_name=from_name,
                          in_reply_to=in_reply_to, references=references)
        payload: dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii")}
        if thread_id:
            payload["threadId"] = thread_id
        result = self._execute(self.service.users().messages().send(userId="me", body=payload))
        # Gmail may rewrite Message-ID; read back the real one for correct threading later.
        rfc_id = mime["Message-ID"]
        try:
            meta = self._execute(self.service.users().messages().get(
                userId="me", id=result["id"], format="metadata", metadataHeaders=["Message-ID"]))
            for header in (meta.get("payload") or {}).get("headers") or []:
                if header.get("name", "").lower() == "message-id":
                    rfc_id = header.get("value", rfc_id)
        except GmailError:
            pass
        return SentMessage(result["id"], result.get("threadId", ""), rfc_id)

    def list_recent_inbound(self, days: int) -> list[MessageRef]:
        query = f"newer_than:{days}d -from:me -in:chats -in:drafts"
        refs: list[MessageRef] = []
        page_token = None
        while len(refs) < 1000:
            resp = self._execute(self.service.users().messages().list(
                userId="me", q=query, maxResults=200, pageToken=page_token))
            refs += [MessageRef(m["id"], m.get("threadId", "")) for m in resp.get("messages") or []]
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return refs

    def get_message(self, message_id: str) -> InboxMessage:
        raw = self._execute(self.service.users().messages().get(userId="me", id=message_id, format="full"))
        return parse_gmail_message(raw)

    def get_thread(self, thread_id: str) -> list[InboxMessage]:
        raw = self._execute(self.service.users().threads().get(userId="me", id=thread_id, format="full"))
        return [parse_gmail_message(m) for m in raw.get("messages") or []]


def get_gmail_client(settings: Any) -> GmailClient:
    """Demo mode never touches the real Gmail account."""
    if settings.demo_mode:
        from app.email.demo_mailbox import DemoMailbox
        return DemoMailbox(settings.resolve_path(settings.demo_mailbox_path))
    return GoogleGmailClient(settings.resolve_path(settings.gmail_credentials_path),
                             settings.resolve_path(settings.gmail_token_path))
