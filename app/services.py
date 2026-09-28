"""Lazy service container - wires services together for jobs, API and dashboard.

Heavy or credentialed dependencies (AI provider, Gmail client) are only built
when a feature actually needs them.
"""

from __future__ import annotations

from functools import cached_property

from sqlalchemy.orm import Session

from app.ai.service import AIService, build_ai_service
from app.config.settings import Settings
from app.email.gmail_client import GmailClient, get_gmail_client
from app.email.inbox_monitor import InboxMonitor
from app.email.sender import SendService
from app.leads.lead_service import LeadService
from app.leads.researcher import CompanyResearcher
from app.outreach.approval_service import ApprovalService
from app.outreach.campaign_service import CampaignService
from app.outreach.email_generator import EmailGenerator
from app.outreach.followup_service import FollowupService
from app.outreach.reply_classifier import ReplyClassifier
from app.outreach.reply_handler import ReplyProcessor


class Services:
    def __init__(self, session: Session, settings: Settings,
                 ai: AIService | None = None, gmail: GmailClient | None = None) -> None:
        self.session = session
        self.settings = settings
        if ai is not None:
            self.__dict__["ai"] = ai
        if gmail is not None:
            self.__dict__["gmail"] = gmail

    @cached_property
    def ai(self) -> AIService:
        return build_ai_service(self.session, self.settings)

    @cached_property
    def gmail(self) -> GmailClient:
        return get_gmail_client(self.settings)

    @cached_property
    def campaigns(self) -> CampaignService:
        return CampaignService(self.session, self.settings)

    @cached_property
    def leads(self) -> LeadService:
        return LeadService(self.session)

    @cached_property
    def researcher(self) -> CompanyResearcher:
        return CompanyResearcher(self.session, self.settings, self.ai)

    @cached_property
    def generator(self) -> EmailGenerator:
        return EmailGenerator(self.session, self.settings, self.ai)

    @cached_property
    def approvals(self) -> ApprovalService:
        return ApprovalService(self.session)

    @cached_property
    def sender(self) -> SendService:
        return SendService(self.session, self.settings, self.gmail)

    @cached_property
    def followups(self) -> FollowupService:
        return FollowupService(self.session, self.settings, self.generator)

    @cached_property
    def inbox(self) -> InboxMonitor:
        processor = ReplyProcessor(self.session, self.settings, ReplyClassifier(self.ai), self.generator)
        return InboxMonitor(self.session, self.settings, self.gmail, processor)
