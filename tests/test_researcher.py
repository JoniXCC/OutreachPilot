"""Company research tests - websites are served by httpx.MockTransport."""

import httpx
import pytest

from app.ai.mock_provider import MockProvider
from app.ai.service import AIService
from app.database.models import AIUsage, LeadStatus
from app.leads.researcher import CompanyResearcher, ResearchError, WebFetcher, extract_page

HOME = """<html><head><title>ABC Dental Berlin</title>
<meta name="description" content="Family dental clinic with three locations in Berlin."></head>
<body>
<nav><a href="/">Home</a><a href="/ueber-uns">Über uns</a></nav>
<div class="cookie-banner">We use cookies to improve your experience on this website.</div>
<main>
<h1>Welcome to ABC Dental, your family dentist in Berlin</h1>
<p>Our team treats patients of all ages across three clinics in Berlin Mitte, Pankow and Wedding.</p>
<p>Patients can book appointments by phone or with our online booking form every weekday.</p>
<script>var tracking = "should never appear in text";</script>
</main>
<footer>Impressum Datenschutz Copyright 2026 ABC Dental GmbH all rights reserved</footer>
</body></html>"""

ABOUT = """<html><body><main><p>Founded in 2009, ABC Dental has grown to a team of 18 people
across three locations in Berlin.</p></main></body></html>"""


def site_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if request.url.host == "abcdental.de":
        if path in ("", "/"):
            return httpx.Response(200, html=HOME)
        if path == "/ueber-uns":
            return httpx.Response(200, html=ABOUT)
    if request.url.host == "redirect.de":
        return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
    if request.url.host == "pdf.de":
        return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
    if request.url.host == "slow.de":
        raise httpx.ReadTimeout("timeout", request=request)
    return httpx.Response(404)


@pytest.fixture
def fetcher(settings):
    client = httpx.Client(transport=httpx.MockTransport(site_handler), follow_redirects=False)
    return WebFetcher(settings, client=client, resolve_dns=False, sleep=lambda s: None)


@pytest.fixture
def researcher(session, settings, fetcher):
    return CompanyResearcher(session, settings, AIService(session, MockProvider()), fetcher)


def test_extract_page_strips_noise():
    page = extract_page(HOME, "https://abcdental.de")
    assert "ABC Dental Berlin" == page.title
    assert "three locations" in page.description
    assert "online booking form" in page.text
    assert "cookies" not in page.text
    assert "tracking" not in page.text
    assert "Impressum" not in page.text
    assert page.about_url == "https://abcdental.de/ueber-uns"


def test_research_success_and_status(session, researcher, lead_factory):
    lead = lead_factory(website="https://abcdental.de")
    outcome = researcher.research(lead)
    assert outcome.status == "researched"
    assert lead.status == LeadStatus.RESEARCHED
    assert lead.research["company_summary"]
    assert 0 <= lead.research["confidence"] <= 1
    assert lead.researched_at is not None and lead.research_error is None


def test_research_is_never_repeated(session, researcher, lead_factory):
    lead = lead_factory(website="https://abcdental.de")
    researcher.research(lead)
    calls_before = session.query(AIUsage).count()
    assert researcher.research(lead).status == "cached"
    assert session.query(AIUsage).count() == calls_before


def test_research_reused_for_same_domain(session, settings, researcher, lead_factory):
    from app.database.models import Campaign
    other = Campaign(name="Other", company_name="X", product_name="Y")
    session.add(other)
    session.commit()
    first = lead_factory(website="https://abcdental.de")
    researcher.research(first)
    from tests.conftest import make_lead
    second = make_lead(session, other, "tom@abcdental.de", website="https://abcdental.de")
    outcome = researcher.research(second)
    assert outcome.status == "reused"
    assert second.research == first.research


@pytest.mark.parametrize("website,reason", [
    ("https://redirect.de", "blocked URL"),        # redirect to internal address (SSRF)
    ("https://pdf.de", "not an HTML page"),
    ("https://slow.de", "timeout"),
    ("https://missing.de", "HTTP 404"),
    ("ftp://abcdental.de", "blocked URL"),
])
def test_research_fails_gracefully(session, researcher, lead_factory, website, reason):
    lead = lead_factory(website=website)
    outcome = researcher.research(lead)
    assert outcome.status == "failed"
    assert reason in lead.research_error
    assert lead.researched_at is not None          # not retried on every run
    assert lead.status == LeadStatus.RESEARCHED   # pipeline continues with light personalisation


def test_research_without_website_or_company_domain(session, researcher, lead_factory):
    lead = lead_factory(email="solo@gmail.com", website="")
    lead.domain = "gmail.com"
    assert researcher.research(lead).status == "failed"


def test_research_ai_failure_uses_metadata(session, settings, fetcher, lead_factory):
    from app.ai.provider import AIError

    class Broken(MockProvider):
        def generate_text(self, *a, **k):
            raise AIError("quota")

    lead = lead_factory(website="https://abcdental.de")
    r = CompanyResearcher(session, settings, AIService(session, Broken()), fetcher)
    outcome = r.research(lead)
    assert outcome.status == "fallback"
    assert "three locations" in lead.research_summary


def test_fetcher_blocks_private_ip(fetcher):
    with pytest.raises(ResearchError):
        fetcher.fetch("http://10.0.0.5/")
