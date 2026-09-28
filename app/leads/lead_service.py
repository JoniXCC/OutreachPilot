"""Lead management: validated creation, bulk import, manual status changes."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config.logging_config import log_event
from app.database.models import Campaign, Lead, LeadStatus, SuppressionReason
from app.database.repositories import LeadRepository, SuppressionRepository
from app.leads.deduplicator import LeadDeduplicator, lead_domain
from app.leads.importer import LeadInput, RowIssue, company_from_domain, parse_csv, parse_pasted_list
from app.outreach.compliance import suppress_lead
from app.outreach.status_machine import transition_lead
from app.utils.helpers import normalize_email, normalize_name, normalize_website
from app.utils.validators import is_valid_email, sanitize_single_line, sanitize_text


@dataclass
class ImportReport:
    imported: list[int] = field(default_factory=list)
    duplicates: list[RowIssue] = field(default_factory=list)
    invalid: list[RowIssue] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (f"{len(self.imported)} imported, {len(self.duplicates)} duplicates skipped, "
                f"{len(self.invalid)} invalid rows")


class LeadService:
    def __init__(self, session: Session, allow_multiple_contacts_per_domain: bool = False) -> None:
        self.session = session
        self.repo = LeadRepository(session)
        self.dedupe = LeadDeduplicator(session, allow_multiple_contacts_per_domain)

    # ------------------------------------------------------------------ creation
    def add_lead(self, campaign: Campaign, data: LeadInput, source: str = "manual"
                 ) -> tuple[Lead | None, RowIssue | None, bool]:
        """Validate and insert one lead.

        Returns ``(lead, issue, is_duplicate)``. Never raises for bad input.
        """
        email = sanitize_single_line(data.email, 254)
        row = data.row_number
        if not is_valid_email(email):
            return None, RowIssue(row, email, "invalid email address"), False
        if SuppressionRepository(self.session).is_suppressed(email):
            return None, RowIssue(row, email, "address is on the suppression list"), False

        company = sanitize_single_line(data.company_name, 160) or company_from_domain(email)
        if not company:
            return None, RowIssue(row, email, "company name missing (and not derivable from a free-mail address)"), False
        contact = sanitize_single_line(data.contact_name, 160)
        website = normalize_website(sanitize_single_line(data.website, 255))

        dup = self.dedupe.check(campaign.id, email, company, contact, website)
        if dup.is_duplicate:
            log_event("lead_duplicate_skipped", campaign_id=campaign.id, reason=dup.reason,
                      existing_lead_id=dup.existing_lead_id)
            return None, RowIssue(row, email, dup.reason), True

        lead = Lead(
            campaign_id=campaign.id,
            company_name=company,
            website=website,
            domain=lead_domain(email, website),
            industry=sanitize_single_line(data.industry, 120) or campaign.target_industry,
            location=sanitize_single_line(data.location, 160),
            contact_name=contact,
            contact_role=sanitize_single_line(data.contact_role, 120),
            email=email,
            email_normalized=normalize_email(email),
            company_key=normalize_name(company),
            contact_key=normalize_name(contact),
            source=source,
            notes=sanitize_text(data.notes, 1000),
            status=LeadStatus.NEW,
        )
        try:
            with self.session.begin_nested():
                self.session.add(lead)
                self.session.flush()
        except IntegrityError:
            return None, RowIssue(row, email, "duplicate email"), True
        log_event("lead_imported", lead_id=lead.id, campaign_id=campaign.id, source=source)
        return lead, None, False

    def import_rows(self, campaign: Campaign, rows: list[LeadInput], source: str,
                    parse_issues: list[RowIssue] | None = None) -> ImportReport:
        report = ImportReport(invalid=list(parse_issues or []))
        for row in rows:
            lead, issue, is_dup = self.add_lead(campaign, row, source)
            if lead:
                report.imported.append(lead.id)
            elif is_dup and issue:
                report.duplicates.append(issue)
            elif issue:
                report.invalid.append(issue)
        self.session.commit()
        return report

    def import_csv(self, campaign: Campaign, data: str | bytes) -> ImportReport:
        parsed = parse_csv(data)  # raises ImportFormatError for unusable files
        return self.import_rows(campaign, parsed.rows, "csv", parsed.issues)

    def import_pasted(self, campaign: Campaign, text: str) -> ImportReport:
        parsed = parse_pasted_list(text)
        return self.import_rows(campaign, parsed.rows, "pasted", parsed.issues)

    # ------------------------------------------------------------------ updates
    def set_status(self, lead: Lead, status: LeadStatus | str, clear_review: bool = False) -> None:
        transition_lead(lead, status)
        if clear_review:
            lead.human_review_required = False
        if status in (LeadStatus.NOT_INTERESTED, LeadStatus.COMPLETED, LeadStatus.BOUNCED):
            lead.next_followup_at = None
        self.session.commit()

    def mark_do_not_contact(self, lead: Lead, note: str = "manual") -> None:
        suppress_lead(self.session, lead, SuppressionReason.MANUAL, note)
        self.session.commit()

    def delete(self, lead: Lead) -> None:
        self.session.delete(lead)
        self.session.commit()


def leads_to_csv(leads: list[Lead]) -> str:
    """Export leads (e.g. to open in Google Sheets / Excel)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    columns = ["id", "company_name", "website", "contact_name", "contact_role", "email",
               "industry", "location", "status", "reply_status", "followup_count",
               "last_contacted_at", "next_followup_at", "human_review_required", "do_not_contact",
               "research_summary"]
    writer.writerow(columns)
    for lead in leads:
        writer.writerow([getattr(lead, col) if getattr(lead, col) is not None else "" for col in columns])
    return buffer.getvalue()
