"""Campaign creation, editing, lifecycle controls and ICP definition."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.ai.prompts import icp_prompt
from app.ai.service import AIService
from app.config.logging_config import log_event
from app.config.settings import Settings
from app.database.models import Campaign, CampaignStatus, MessageStatus
from app.database.repositories import CampaignRepository, LeadRepository, MessageRepository, ReplyRepository
from app.outreach.status_machine import transition_campaign
from app.utils.validators import sanitize_single_line, sanitize_text


class CampaignInput(BaseModel):
    """Validated user input for a campaign (dashboard form / API)."""

    name: str = Field(min_length=2, max_length=120)
    company_name: str = Field(min_length=1, max_length=120)
    sender_name: str = Field(default="", max_length=120)
    product_name: str = Field(min_length=2, max_length=160)
    product_description: str = Field(default="", max_length=2000)
    value_proposition: str = Field(default="", max_length=1000)
    target_industry: str = Field(default="", max_length=160)
    target_company_size: str = Field(default="", max_length=80)
    target_locations: str = Field(default="", max_length=255)
    target_roles: str = Field(default="", max_length=255)
    tone: str = Field(default="friendly, professional, concise", max_length=120)
    call_to_action: str = Field(default="Would you be open to a short call next week?", max_length=255)
    max_emails_per_day: int = Field(default=20, ge=1, le=200)
    max_followups: int = Field(default=2, ge=0, le=5)
    followup_1_days: int = Field(default=3, ge=1, le=60)
    followup_2_days: int = Field(default=5, ge=1, le=60)
    auto_send: bool = False
    is_demo: bool = False

    @field_validator("*", mode="before")
    @classmethod
    def _clean(cls, value: Any, info: Any) -> Any:
        if isinstance(value, str):
            multiline = info.field_name in {"product_description", "value_proposition"}
            return sanitize_text(value, 2000) if multiline else sanitize_single_line(value, 255)
        return value


class CampaignService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.repo = CampaignRepository(session)

    def create(self, data: CampaignInput) -> Campaign:
        if self.repo.get_by_name(data.name):
            raise ValueError(f"A campaign named '{data.name}' already exists")
        values = data.model_dump()
        # A campaign can never exceed the global safety limits.
        values["max_emails_per_day"] = min(values["max_emails_per_day"], self.settings.max_emails_per_day)
        values["max_followups"] = min(values["max_followups"], self.settings.max_followups)
        campaign = Campaign(**values, status=CampaignStatus.DRAFT)
        self.session.add(campaign)
        self.session.commit()
        log_event("campaign_created", campaign_id=campaign.id, name=campaign.name)
        return campaign

    def update(self, campaign: Campaign, data: CampaignInput) -> Campaign:
        other = self.repo.get_by_name(data.name)
        if other and other.id != campaign.id:
            raise ValueError(f"A campaign named '{data.name}' already exists")
        values = data.model_dump()
        values["max_emails_per_day"] = min(values["max_emails_per_day"], self.settings.max_emails_per_day)
        values["max_followups"] = min(values["max_followups"], self.settings.max_followups)
        icp_fields = ("product_name", "product_description", "value_proposition", "target_industry",
                      "target_company_size", "target_locations", "target_roles")
        if any(getattr(campaign, f) != values[f] for f in icp_fields):
            campaign.icp_json = None  # targeting changed -> ICP must be regenerated
        for key, value in values.items():
            setattr(campaign, key, value)
        self.session.commit()
        return campaign

    # ------------------------------------------------------------------ lifecycle
    def set_status(self, campaign: Campaign, status: CampaignStatus | str) -> None:
        transition_campaign(campaign, status)
        if status == CampaignStatus.STOPPED:
            # Stop control: nothing queued for this campaign may go out any more.
            for msg in MessageRepository(self.session).by_status(
                    [MessageStatus.PENDING_APPROVAL, MessageStatus.APPROVED], campaign.id):
                msg.status = MessageStatus.CANCELLED
                msg.error = "campaign stopped"
            for lead in campaign.leads:
                lead.next_followup_at = None
        self.session.commit()
        log_event("campaign_status_changed", campaign_id=campaign.id, status=str(status))

    def delete(self, campaign: Campaign) -> None:
        self.session.delete(campaign)
        self.session.commit()

    # ------------------------------------------------------------------ ICP (one cached AI call)
    def generate_icp(self, campaign: Campaign, ai: AIService, force: bool = False) -> dict[str, Any]:
        if campaign.icp and not force:
            return campaign.icp
        data = ai.generate_json(
            "define_icp",
            icp_prompt(campaign.product_name, campaign.product_description, campaign.value_proposition,
                       campaign.target_industry, campaign.target_company_size,
                       campaign.target_locations, campaign.target_roles),
            use_cache=not force, max_tokens=300,
        )
        icp = {
            "summary": str(data.get("summary", ""))[:400],
            "pain_points": [str(x)[:120] for x in (data.get("pain_points") or [])][:3],
            "qualifying_signals": [str(x)[:120] for x in (data.get("qualifying_signals") or [])][:3],
            "disqualifiers": [str(x)[:120] for x in (data.get("disqualifiers") or [])][:3],
        }
        campaign.icp = icp
        self.session.commit()
        return icp

    # ------------------------------------------------------------------ analytics
    def stats(self, campaign_id: int | None = None) -> dict[str, Any]:
        """Deterministic campaign metrics (no tracking pixels, no invented numbers)."""
        leads = LeadRepository(self.session)
        messages = MessageRepository(self.session)
        replies = ReplyRepository(self.session)
        status_counts = leads.status_counts(campaign_id)
        sent_messages = messages.by_status([MessageStatus.SENT], campaign_id)
        initial_sent = sum(1 for m in sent_messages if m.kind == "INITIAL")
        followups_sent = sum(1 for m in sent_messages if m.kind == "FOLLOWUP")
        categories = replies.category_counts(campaign_id)
        replied = replies.replied_lead_count(campaign_id)
        interested = categories.get("INTERESTED", 0)
        positive = interested + categories.get("MORE_INFORMATION", 0) + categories.get("QUESTION", 0)
        bounced = status_counts.get("BOUNCED", 0)
        return {
            "total_leads": sum(status_counts.values()),
            "status_counts": status_counts,
            "emails_sent": len(sent_messages),
            "initial_sent": initial_sent,
            "followups_sent": followups_sent,
            "bounced": bounced,
            "delivered_estimate": max(initial_sent - bounced, 0),
            "replied_leads": replied,
            "interested": interested,
            "not_interested": categories.get("NOT_INTERESTED", 0) + categories.get("UNSUBSCRIBE", 0),
            "reply_categories": categories,
            "reply_rate": round(replied / initial_sent, 3) if initial_sent else 0.0,
            "positive_reply_rate": round(positive / initial_sent, 3) if initial_sent else 0.0,
            "pending_approvals": len(messages.pending_approval(campaign_id)),
            "human_review": len(leads.list(campaign_id, human_review=True)),
            "followups_due": status_counts.get("FOLLOWUP_DUE", 0),
        }
