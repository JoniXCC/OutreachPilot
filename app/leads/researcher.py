"""Lightweight, polite company research.

Flow per lead (at most ``RESEARCH_MAX_PAGES`` requests, usually 1-2):

1. reuse cached research for the lead, or for another lead with the same domain
2. fetch the homepage (SSRF-checked, robots.txt respected, size/time capped)
3. optionally fetch one "about"/"services" page on the same host
4. strip scripts, navigation, cookie banners, footers; collapse whitespace
5. send at most ``RESEARCH_MAX_CHARS`` characters to the AI for a JSON summary

Any failure is recorded on the lead and never propagates - one broken website
must not stop a batch.
"""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup
from sqlalchemy.orm import Session

from app.ai.prompts import research_prompt
from app.ai.provider import AIError
from app.ai.service import AIService
from app.config.logging_config import get_logger, log_event
from app.config.settings import Settings
from app.database.models import Lead, LeadStatus
from app.database.repositories import LeadRepository
from app.outreach.status_machine import can_transition_lead
from app.utils.helpers import is_free_email_domain, truncate, utcnow
from app.utils.validators import ValidationError, validate_public_url

logger = get_logger("research")

NOISE_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "nav", "footer", "header",
              "aside", "button", "select", "input", "template", "canvas", "video", "audio")
NOISE_TOKEN = re.compile(
    r"^(cookie.*|.*cookie|consent.*|gdpr.*|cmp.*|popup|modal|newsletter.*|breadcrumbs?|"
    r"skip-link|social.*|share.*|site-footer|site-header|main-nav.*|navbar.*|menu)$",
    re.IGNORECASE,
)
ABOUT_KEYWORDS = ("about", "ueber-uns", "uber-uns", "über-uns", "unternehmen", "company", "team",
                  "who-we-are", "services", "leistungen", "praxis", "our-story")
MAX_REDIRECTS = 3


class ResearchError(RuntimeError):
    pass


@dataclass
class PageContent:
    url: str
    title: str
    description: str
    text: str
    about_url: str | None = None


# --------------------------------------------------------------------------- HTML -> text
def extract_page(html: str, url: str, max_chars: int = 6000) -> PageContent:
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)}) or \
        soup.find("meta", attrs={"property": "og:description"})
    description = (meta.get("content") or "").strip() if meta else ""
    about_url = find_about_link(soup, url)

    for tag in soup(NOISE_TAGS):
        tag.decompose()
    for el in soup.find_all(True):
        if getattr(el, "decomposed", False) or el.attrs is None:
            continue
        tokens = list(el.get("class") or []) + [el.get("id") or "", el.get("role") or ""]
        if any(tok and NOISE_TOKEN.match(tok) for tok in tokens) or el.get("aria-hidden") == "true":
            el.decompose()

    seen: set[str] = set()
    lines: list[str] = []
    for raw in soup.get_text("\n").splitlines():
        line = " ".join(raw.split())
        if len(line) < 25 or line.lower() in seen:
            continue
        seen.add(line.lower())
        lines.append(line)
    return PageContent(url=url, title=title[:200], description=description[:400],
                       text=truncate("\n".join(lines), max_chars), about_url=about_url)


def find_about_link(soup: BeautifulSoup, base_url: str) -> str | None:
    base_host = urlparse(base_url).hostname
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        full = urljoin(base_url, href)
        parsed = urlparse(full)
        if parsed.hostname != base_host or parsed.scheme not in ("http", "https"):
            continue
        haystack = (parsed.path + " " + a.get_text(" ", strip=True)).lower()
        if any(k in haystack for k in ABOUT_KEYWORDS) and full.rstrip("/") != base_url.rstrip("/"):
            return full.split("#")[0]
    return None


# --------------------------------------------------------------------------- fetching
class WebFetcher:
    """Polite HTTP fetcher: rate-limited, size-capped, robots-aware, SSRF-safe."""

    _lock = threading.Lock()
    _last_request_at = 0.0

    def __init__(self, settings: Settings, client: httpx.Client | None = None,
                 resolve_dns: bool = True, sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self.resolve_dns = resolve_dns
        self.sleep = sleep
        self.client = client or httpx.Client(
            timeout=settings.research_timeout_seconds,
            headers={"User-Agent": settings.research_user_agent,
                     "Accept": "text/html,application/xhtml+xml",
                     "Accept-Language": "en,de;q=0.8"},
            follow_redirects=False,
        )
        self._robots: dict[str, RobotFileParser | None] = {}

    def _throttle(self) -> None:
        interval = self.settings.research_min_seconds_between_requests
        with WebFetcher._lock:
            wait = WebFetcher._last_request_at + interval - time.monotonic()
            if wait > 0:
                self.sleep(wait)
            WebFetcher._last_request_at = time.monotonic()

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.settings.research_respect_robots_txt:
            return True
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            parser: RobotFileParser | None = None
            try:
                self._throttle()
                resp = self.client.get(origin + "/robots.txt")
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text[:100_000].splitlines())
            except httpx.HTTPError:
                parser = None  # unreachable robots.txt -> treat as allowed
            self._robots[origin] = parser
        parser = self._robots[origin]
        return parser is None or parser.can_fetch(self.settings.research_user_agent, url)

    def fetch(self, url: str) -> tuple[str, str]:
        """Return ``(final_url, html)`` or raise :class:`ResearchError`."""
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            try:
                validate_public_url(current, resolve_dns=self.resolve_dns)
            except ValidationError as exc:
                raise ResearchError(f"blocked URL: {exc}") from exc
            if not self._allowed_by_robots(current):
                raise ResearchError("disallowed by robots.txt")
            self._throttle()
            try:
                with self.client.stream("GET", current) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise ResearchError("redirect without location")
                        current = urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        raise ResearchError(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "")
                    if "html" not in ctype and "text" not in ctype:
                        raise ResearchError(f"not an HTML page ({ctype or 'unknown type'})")
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in resp.iter_bytes():
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= self.settings.research_max_bytes:
                            break
                    encoding = resp.encoding or "utf-8"
                    return current, b"".join(chunks).decode(encoding, errors="replace")
            except httpx.TimeoutException as exc:
                raise ResearchError("timeout") from exc
            except httpx.HTTPError as exc:
                raise ResearchError(f"network error: {type(exc).__name__}") from exc
        raise ResearchError("too many redirects")


# --------------------------------------------------------------------------- researcher
@dataclass
class ResearchOutcome:
    lead_id: int
    status: str  # "cached" | "reused" | "researched" | "failed" | "fallback"
    detail: str = ""


class CompanyResearcher:
    def __init__(self, session: Session, settings: Settings, ai: AIService,
                 fetcher: WebFetcher | None = None) -> None:
        self.session = session
        self.settings = settings
        self.ai = ai
        self.fetcher = fetcher or WebFetcher(settings)
        self.leads = LeadRepository(session)

    def research(self, lead: Lead, force: bool = False) -> ResearchOutcome:
        # 1. Never research the same lead twice (failures are cached too).
        if lead.researched_at and not force:
            return ResearchOutcome(lead.id, "cached")

        # 2. Same company already researched for another lead -> copy, zero cost.
        donor = None if force else self.leads.find_research_donor(lead.domain, lead.id)
        if donor:
            self._save(lead, donor.research, error=None)
            return ResearchOutcome(lead.id, "reused", f"from lead {donor.id}")

        url = self._target_url(lead)
        if not url:
            self._save(lead, {}, error="no website available")
            return ResearchOutcome(lead.id, "failed", "no website available")

        # 3. Fetch 1-2 pages.
        try:
            pages = self._fetch_pages(url)
        except ResearchError as exc:
            self._save(lead, {}, error=str(exc))
            log_event("research_failed", lead_id=lead.id, url=url, reason=str(exc))
            return ResearchOutcome(lead.id, "failed", str(exc))

        text = self._compose_text(pages)
        if len(text) < 80:
            summary = self._fallback_summary(pages)
            self._save(lead, summary, error="website had little readable text")
            return ResearchOutcome(lead.id, "fallback", "little readable text")

        # 4. One compact AI call.
        campaign = lead.campaign
        try:
            data = self.ai.generate_json(
                "research_summary",
                research_prompt(lead.company_name, pages[0].url, campaign.product_name,
                                campaign.value_proposition, text),
                max_tokens=250,
            )
            summary = self._validate_summary(data)
        except AIError as exc:
            summary = self._fallback_summary(pages)
            self._save(lead, summary, error=f"AI unavailable: {truncate(str(exc), 120)}")
            log_event("research_failed", lead_id=lead.id, reason="ai_error", level=30)
            return ResearchOutcome(lead.id, "fallback", "AI unavailable, used page metadata")

        self._save(lead, summary, error=None)
        log_event("company_researched", lead_id=lead.id, pages=len(pages),
                  chars_sent=len(text), confidence=summary.get("confidence"))
        return ResearchOutcome(lead.id, "researched")

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _target_url(lead: Lead) -> str:
        if lead.website:
            return lead.website
        if lead.domain and not is_free_email_domain(lead.domain):
            return f"https://{lead.domain}"
        return ""

    def _fetch_pages(self, url: str) -> list[PageContent]:
        final_url, html = self.fetcher.fetch(url)
        per_page = self.settings.research_max_chars
        pages = [extract_page(html, final_url, per_page)]
        if self.settings.research_max_pages >= 2 and pages[0].about_url:
            try:
                about_url, about_html = self.fetcher.fetch(pages[0].about_url)
                pages.append(extract_page(about_html, about_url, per_page))
            except ResearchError as exc:
                logger.info("About page skipped for %s: %s", url, exc)
        return pages

    def _compose_text(self, pages: list[PageContent]) -> str:
        parts: list[str] = []
        for page in pages:
            header = " | ".join(p for p in (page.title, page.description) if p)
            if header:
                parts.append(header)
            parts.append(page.text)
        return truncate("\n".join(p for p in parts if p), self.settings.research_max_chars)

    @staticmethod
    def _fallback_summary(pages: list[PageContent]) -> dict[str, Any]:
        first = pages[0] if pages else None
        summary = (first.description or first.title) if first else ""
        return {"company_summary": truncate(summary, 200), "relevant_signal": "",
                "personalization_angle": "", "confidence": 0.2 if summary else 0.0}

    @staticmethod
    def _validate_summary(data: dict[str, Any]) -> dict[str, Any]:
        try:
            confidence = float(data.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.3
        return {
            "company_summary": truncate(str(data.get("company_summary") or ""), 300),
            "relevant_signal": truncate(str(data.get("relevant_signal") or ""), 300),
            "personalization_angle": truncate(str(data.get("personalization_angle") or ""), 200),
            "confidence": max(0.0, min(1.0, confidence)),
        }

    def _save(self, lead: Lead, summary: dict[str, Any], error: str | None) -> None:
        lead.research = summary or None
        parts = [summary.get("company_summary", ""), summary.get("relevant_signal", "")] if summary else []
        lead.research_summary = " ".join(p for p in parts if p) or None
        lead.personalization_notes = (summary or {}).get("personalization_angle") or None
        lead.research_error = error[:255] if error else None
        lead.researched_at = utcnow()
        if lead.status == LeadStatus.NEW and can_transition_lead(lead.status, LeadStatus.RESEARCHED):
            lead.status = LeadStatus.RESEARCHED
        self.session.flush()
