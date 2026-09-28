"""Demo mode: fictional companies on reserved domains + a simulated end-to-end run.

Nothing here can email a real person: addresses use RFC 2606 domains
(example.com / .example), research data is pre-filled (no web requests), and
sending goes to the local demo mailbox.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.config.settings import Settings
from app.database.models import Campaign, CampaignStatus, LeadStatus, MessageKind
from app.database.repositories import CampaignRepository, MessageRepository, SuppressionRepository
from app.email.demo_mailbox import DemoMailbox
from app.leads.importer import LeadInput
from app.services import Services
from app.utils.helpers import utcnow
from app.utils.validators import is_reserved_domain

DEMO_CAMPAIGN = "Demo - AI Reception Assistant"

DEMO_LEADS = [
    # company, contact, role, email, industry, location, research summary, signal
    ("Northstar Dental", "Sarah Klein", "Practice Manager", "sarah.klein@northstar-dental.example",
     "Dental clinic", "Berlin, Germany",
     "Family dental practice with three clinics in Berlin.",
     "Website asks patients to call reception for appointment and insurance questions."),
    ("BrightDesk Software", "Tom Becker", "Head of Customer Success", "tom@brightdesk.example",
     "B2B SaaS", "Hamburg, Germany",
     "Helpdesk software vendor for small agencies.",
     "Support page lists phone support hours only on weekdays."),
    ("Harbor Analytics", "Mia Lopez", "COO", "mia.lopez@harbor-analytics.example",
     "Data consultancy", "Munich, Germany",
     "Analytics consultancy serving mid-sized retailers.",
     ""),
    ("Atlas Fitness", "Jonas Weber", "Owner", "jonas@atlas-fitness.example",
     "Fitness studio", "Cologne, Germany",
     "Independent gym with two locations and group classes.",
     "Class bookings and membership questions are handled by the front desk."),
    ("Lakeside Physio", "Anna Schmidt", "Owner", "anna@lakeside-physio.example",
     "Physiotherapy", "Leipzig, Germany",
     "Physiotherapy practice offering sports rehab.",
     "Site says new patients should phone to check therapist availability."),
    ("Pinewood Vets", "Lukas Braun", "Practice Manager", "lukas@pinewood-vets.example",
     "Veterinary clinic", "Stuttgart, Germany",
     "Veterinary clinic for small animals with emergency hours.",
     "FAQ page covers opening hours, vaccinations and emergency contacts."),
]

# Which simulated reply each demo lead sends (None = no reply -> follow-up path)
DEMO_REPLIES = {
    "Northstar Dental": "interested",
    "BrightDesk Software": "question",
    "Harbor Analytics": "not_interested",
    "Atlas Fitness": "out_of_office",
    "Lakeside Physio": "unsubscribe",
    "Pinewood Vets": None,
}


def seed_demo(session: Session, settings: Settings) -> Campaign:
    """Create (or return) the demo campaign with fictional leads and pre-filled research."""
    svc = Services(session, settings)
    existing = CampaignRepository(session).get_by_name(DEMO_CAMPAIGN)
    if existing:
        return existing
    from app.outreach.campaign_service import CampaignInput
    campaign = svc.campaigns.create(CampaignInput(
        name=DEMO_CAMPAIGN,
        company_name="SmileDesk AI",
        sender_name=settings.sender_name,
        product_name="AI reception assistant",
        product_description="A website and phone-line assistant that answers routine customer "
                            "questions (opening hours, bookings, prices) around the clock.",
        value_proposition="It answers common customer questions automatically and reduces "
                          "repetitive calls for reception staff",
        target_industry="Local service businesses (clinics, studios)",
        target_company_size="2-20 employees",
        target_locations="Germany",
        target_roles="Owner, Practice Manager",
        tone="friendly, plain, professional",
        call_to_action="Would you be open to a 10-minute demo next week?",
        max_emails_per_day=settings.max_emails_per_day,
        is_demo=True,
    ))
    svc.campaigns.set_status(campaign, CampaignStatus.ACTIVE)
    for company, contact, role, email, industry, location, summary, signal in DEMO_LEADS:
        lead, _, _ = svc.leads.add_lead(campaign, LeadInput(
            email=email, company_name=company, contact_name=contact, contact_role=role,
            industry=industry, location=location,
            website=f"https://{email.split('@')[1]}"), source="demo")
        if lead:
            lead.research = {"company_summary": summary, "relevant_signal": signal,
                             "personalization_angle": "Relate to reducing repetitive enquiries.",
                             "confidence": 0.8 if signal else 0.5}
            lead.research_summary = f"{summary} {signal}".strip()
            lead.personalization_notes = "Relate to reducing repetitive enquiries."
            lead.researched_at = utcnow()
            lead.status = LeadStatus.RESEARCHED
    session.commit()
    return campaign


@dataclass
class DemoReport:
    drafts: int
    sent: int
    replies: int
    summary: dict
    ai_provider: str


def run_demo_flow(session: Session, settings: Settings) -> DemoReport:
    """Seed -> draft -> approve (as the demo operator) -> send to demo mailbox ->
    simulate replies -> classify -> apply rules. Requires DEMO_MODE."""
    if not settings.demo_mode:
        raise RuntimeError("run_demo_flow only runs with DEMO_MODE=true")
    campaign = seed_demo(session, settings)
    fast = settings.model_copy(update={"min_seconds_between_emails": 0})
    svc = Services(session, fast)
    if not isinstance(svc.gmail, DemoMailbox):
        raise RuntimeError("demo flow requires the demo mailbox")

    drafts = 0
    for lead in campaign.leads:
        if svc.generator.generate_initial(lead):
            drafts += 1
    session.commit()
    # In the demo the operator approves everything; in real use this happens in the dashboard.
    for message in MessageRepository(session).pending_approval(campaign.id):
        if message.kind == MessageKind.INITIAL:  # suggested replies stay with the human
            svc.approvals.approve(message)
    sent = svc.sender.process_queue().sent

    replies = 0
    for lead in campaign.leads:
        scenario = DEMO_REPLIES.get(lead.company_name)
        if scenario and lead.gmail_thread_id and not lead.replies:
            svc.gmail.simulate_scenario(lead.gmail_thread_id, lead.email, scenario)
            replies += 1
    svc.inbox.check()
    session.commit()
    return DemoReport(drafts, sent, replies, svc.campaigns.stats(campaign.id), svc.ai.provider_label)


def reset_demo(session: Session, settings: Settings) -> None:
    campaign = CampaignRepository(session).get_by_name(DEMO_CAMPAIGN)
    if campaign:
        session.delete(campaign)
        session.commit()
    # Demo addresses live on reserved domains, so clearing their suppression is safe.
    for entry in SuppressionRepository(session).list():
        if entry.email and is_reserved_domain(entry.email):
            session.delete(entry)
    session.commit()
    path = settings.resolve_path(settings.demo_mailbox_path)
    path.unlink(missing_ok=True)

