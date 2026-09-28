import pytest

from app.database.models import Lead, LeadStatus
from app.database.repositories import SuppressionRepository
from app.leads.deduplicator import LeadDeduplicator
from app.leads.importer import ImportFormatError, LeadInput, parse_csv, parse_pasted_list
from app.leads.lead_service import LeadService, leads_to_csv
from app.utils.validators import is_reserved_domain, is_valid_email


# ------------------------------------------------------------------ email validation
@pytest.mark.parametrize("email", ["sarah@abcdental.de", "first.last+tag@sub.company.co.uk",
                                   "o'neil@example.com"])
def test_valid_emails(email):
    assert is_valid_email(email)


@pytest.mark.parametrize("email", ["", "plainaddress", "@no-local.com", "a@b", "a@@b.com",
                                   "a b@c.com", "x" * 250 + "@c.com", None])
def test_invalid_emails(email):
    assert not is_valid_email(email)


def test_reserved_domains():
    assert is_reserved_domain("anna@example.com")
    assert is_reserved_domain("bob@shop.test")
    assert not is_reserved_domain("bob@realcompany.de")


# ------------------------------------------------------------------ CSV parsing
CSV_TEXT = """company_name,website,contact_name,contact_role,email,industry,location
ABC Dental,abcdental.de,Sarah Klein,Practice Manager,sarah@abcdental.de,Dental,Berlin
Bad Row Inc,bad.com,Bob,Owner,not-an-email,Dental,Hamburg
,,,,,,
No Email Ltd,noemail.de,Eve,Owner,,Dental,Munich
"""


def test_parse_csv_reports_missing_email():
    result = parse_csv(CSV_TEXT)
    assert [r.email for r in result.rows] == ["sarah@abcdental.de", "not-an-email"]
    assert len(result.issues) == 1 and result.issues[0].reason == "missing email"


def test_parse_csv_semicolon_and_aliases():
    text = "Company;E-Mail;Name;Title\nNorthstar;anna@northstar.io;Anna;CEO\n"
    rows = parse_csv(text.encode("utf-8-sig")).rows
    assert rows[0].company_name == "Northstar"
    assert rows[0].contact_role == "CEO"


def test_parse_csv_without_email_column_fails_cleanly():
    with pytest.raises(ImportFormatError):
        parse_csv("company,website\nABC,abc.de\n")
    with pytest.raises(ImportFormatError):
        parse_csv("")


def test_parse_pasted_list_formats():
    text = """sarah@abcdental.de
Tom Meyer <tom@meyer-physio.de>, Meyer Physio
Lisa Braun, Owner, lisa@braun-dental.de, Braun Dental
this line has no address"""
    result = parse_pasted_list(text)
    assert len(result.rows) == 3 and len(result.issues) == 1
    assert result.rows[1].contact_name == "Tom Meyer"
    assert result.rows[1].company_name == "Meyer Physio"
    assert result.rows[2].contact_role == "Owner"


# ------------------------------------------------------------------ import + dedupe
def test_import_csv_reports_invalid_rows(session, campaign):
    report = LeadService(session).import_csv(campaign, CSV_TEXT)
    assert len(report.imported) == 1
    reasons = sorted(i.reason for i in report.invalid)
    assert reasons == ["invalid email address", "missing email"]


def test_duplicate_email_blocked(session, campaign):
    svc = LeadService(session)
    svc.import_rows(campaign, [LeadInput(email="sarah@abcdental.de", company_name="ABC Dental")], "csv")
    report = svc.import_rows(campaign, [LeadInput(email="  SARAH@AbcDental.de ", company_name="ABC")], "csv")
    assert report.imported == [] and report.duplicates[0].reason == "duplicate email"


def test_duplicate_domain_blocked_but_freemail_allowed(session, campaign):
    svc = LeadService(session)
    svc.import_rows(campaign, [LeadInput(email="sarah@abcdental.de", company_name="ABC Dental")], "csv")
    report = svc.import_rows(campaign, [
        LeadInput(email="tom@abcdental.de", company_name="ABC Dental", contact_name="Tom"),
        LeadInput(email="one@gmail.com", company_name="Solo Dentist A", contact_name="A"),
        LeadInput(email="two@gmail.com", company_name="Solo Dentist B", contact_name="B"),
    ], "csv")
    assert len(report.imported) == 2
    assert "domain" in report.duplicates[0].reason


def test_duplicate_company_contact_blocked(session, campaign):
    svc = LeadService(session)
    svc.import_rows(campaign, [LeadInput(email="a@gmail.com", company_name="ABC Dental GmbH",
                                         contact_name="Sarah Klein")], "csv")
    dup = LeadDeduplicator(session).check(campaign.id, "b@gmail.com", "abc dental", "SARAH  KLEIN")
    assert dup.is_duplicate and "company + contact" in dup.reason


def test_same_email_allowed_in_other_campaign(session, campaign):
    from app.database.models import Campaign
    other = Campaign(name="Other", company_name="X", product_name="Y")
    session.add(other)
    session.commit()
    svc = LeadService(session)
    svc.import_rows(campaign, [LeadInput(email="sarah@abcdental.de", company_name="ABC")], "csv")
    report = svc.import_rows(other, [LeadInput(email="sarah@abcdental.de", company_name="ABC")], "csv")
    assert len(report.imported) == 1


def test_suppressed_address_not_imported(session, campaign):
    SuppressionRepository(session).add_email("gone@abcdental.de")
    report = LeadService(session).import_rows(campaign, [LeadInput(email="gone@abcdental.de")], "csv")
    assert report.imported == [] and "suppression" in report.invalid[0].reason


def test_company_derived_from_domain_and_sanitised(session, campaign):
    lead, issue, _ = LeadService(session).add_lead(
        campaign, LeadInput(email="info@harbor-analytics.io", contact_name="Mia\x00 Lopez\n"))
    assert issue is None
    assert lead.company_name == "Harbor Analytics"
    assert lead.contact_name == "Mia Lopez"
    assert lead.status == LeadStatus.NEW


def test_export_csv(session, campaign, lead_factory):
    lead_factory()
    out = leads_to_csv(list(session.query(Lead)))
    assert "sarah@abcdental.de" in out and out.startswith("id,company_name")
