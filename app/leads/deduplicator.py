"""Duplicate-lead detection (pure Python + indexed DB lookups, no AI).

A lead is a duplicate within a campaign when any of these match an existing lead:

1. normalised email address
2. company domain (skipped for free-mail domains like gmail.com, unless
   multiple contacts per company are explicitly allowed)
3. normalised company name + contact name
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.database.repositories import LeadRepository
from app.utils.helpers import (
    email_domain, is_free_email_domain, normalize_domain, normalize_email, normalize_name,
)


@dataclass(frozen=True)
class DuplicateCheck:
    is_duplicate: bool
    reason: str = ""
    existing_lead_id: int | None = None


def lead_domain(email: str, website: str = "") -> str:
    """Company domain used for dedupe: email domain, or website if email is free-mail."""
    domain = email_domain(email)
    if domain and not is_free_email_domain(domain):
        return domain
    return normalize_domain(website)


class LeadDeduplicator:
    def __init__(self, session: Session, allow_multiple_contacts_per_domain: bool = False) -> None:
        self.leads = LeadRepository(session)
        self.allow_same_domain = allow_multiple_contacts_per_domain

    def check(self, campaign_id: int, email: str, company_name: str = "",
              contact_name: str = "", website: str = "") -> DuplicateCheck:
        existing = self.leads.find_by_email(campaign_id, normalize_email(email))
        if existing:
            return DuplicateCheck(True, "duplicate email", existing.id)

        if not self.allow_same_domain:
            domain = lead_domain(email, website)
            existing = self.leads.find_by_domain(campaign_id, domain)
            if existing:
                return DuplicateCheck(True, f"company domain {domain} already in campaign", existing.id)

        existing = self.leads.find_by_company_contact(
            campaign_id, normalize_name(company_name), normalize_name(contact_name)
        )
        if existing:
            return DuplicateCheck(True, "same company + contact already in campaign", existing.id)
        return DuplicateCheck(False)
