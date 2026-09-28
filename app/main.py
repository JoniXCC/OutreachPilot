"""Optional FastAPI layer (local use): health, stats, campaigns, leads, job triggers.

Run with ``python run.py api`` (binds to 127.0.0.1). If ``API_TOKEN`` is set,
every request must include the header ``X-API-Token``.
"""

from __future__ import annotations

import hmac
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from sqlalchemy.orm import Session

from app import __version__
from app.config.logging_config import setup_logging
from app.config.settings import Settings, get_settings
from app.database.database import get_engine, init_db, make_session_factory
from app.database.repositories import CampaignRepository, LeadRepository
from app.outreach.campaign_service import CampaignService
from app.scheduler.jobs import JOBS, run_job


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.resolve_path(settings.log_file))
    yield


app = FastAPI(title="AI Sales Outreach Agent", version=__version__, lifespan=lifespan,
              description="Local API for the outreach agent. Not intended for public exposure.")


def settings_dep() -> Settings:
    return get_settings()


def session_dep(settings: Settings = Depends(settings_dep)) -> Iterator[Session]:
    factory = make_session_factory(init_db(get_engine(settings)))
    with factory() as session:
        yield session


def auth_dep(settings: Settings = Depends(settings_dep),
             x_api_token: str | None = Header(default=None)) -> None:
    expected = settings.api_token.get_secret_value() if settings.api_token else ""
    if expected and not (x_api_token and hmac.compare_digest(x_api_token, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Token")


@app.get("/health")
def health(settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    return {"status": "ok", "version": __version__, "demo_mode": settings.demo_mode,
            "safe_mode": settings.safe_mode, "ai_provider": settings.ai_provider}


@app.get("/stats", dependencies=[Depends(auth_dep)])
def stats(campaign_id: int | None = None, session: Session = Depends(session_dep),
          settings: Settings = Depends(settings_dep)) -> dict[str, Any]:
    return CampaignService(session, settings).stats(campaign_id)


@app.get("/campaigns", dependencies=[Depends(auth_dep)])
def campaigns(session: Session = Depends(session_dep)) -> list[dict[str, Any]]:
    return [{"id": c.id, "name": c.name, "status": c.status, "product": c.product_name,
             "auto_send": c.auto_send, "leads": len(c.leads)} for c in CampaignRepository(session).list()]


@app.get("/leads", dependencies=[Depends(auth_dep)])
def leads(campaign_id: int | None = None, status: str | None = None,
          session: Session = Depends(session_dep)) -> list[dict[str, Any]]:
    rows = LeadRepository(session).list(campaign_id, [status] if status else None)
    return [{"id": lead.id, "company": lead.company_name, "contact": lead.contact_name, "email": lead.email,
             "status": lead.status, "reply_status": lead.reply_status, "followups": lead.followup_count,
             "last_contacted_at": lead.last_contacted_at, "human_review": lead.human_review_required}
            for lead in rows]


@app.post("/jobs/{name}", dependencies=[Depends(auth_dep)])
def trigger_job(name: str) -> dict[str, str]:
    if name != "all" and name not in JOBS:
        raise HTTPException(status_code=404, detail=f"unknown job {name}")
    # wait=False: an API call never blocks for pacing; the next run continues the queue.
    return {"job": name, "result": run_job(name, wait=False)}
