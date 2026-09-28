"""Idempotent background jobs + a lightweight in-process scheduler.

Each job opens its own DB session, is protected by a file lock (so the
dev scheduler and Windows Task Scheduler can't run the same job twice at
once), and never raises - failures are logged and summarised.

Run once:      python run.py run-jobs --job inbox
Run forever:   python run.py scheduler
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from app.ai.provider import AIError
from app.config.logging_config import get_logger, log_event
from app.config.settings import Settings, get_settings
from app.database.database import get_engine, init_db, session_scope
from app.database.repositories import LeadRepository
from app.email.gmail_client import GmailAuthError, GmailError
from app.services import Services

logger = get_logger("jobs")

LOCK_STALE_SECONDS = 30 * 60


class JobLockedError(RuntimeError):
    pass


@contextmanager
def job_lock(name: str, settings: Settings) -> Iterator[None]:
    lock_dir = settings.resolve_path(Path("data/locks"))
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / f"{name}.lock"
    if lock_file.exists() and time.time() - lock_file.stat().st_mtime > LOCK_STALE_SECONDS:
        lock_file.unlink(missing_ok=True)  # crashed run left a stale lock
    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise JobLockedError(f"job '{name}' is already running") from None
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock_file.unlink(missing_ok=True)


# --------------------------------------------------------------------------- job bodies
def research_job(svc: Services, limit: int = 10) -> str:
    leads = LeadRepository(svc.session).needing_research(limit)
    if not leads:
        return "research: nothing to do"
    outcomes: dict[str, int] = {}
    for lead in leads:
        outcome = svc.researcher.research(lead)
        outcomes[outcome.status] = outcomes.get(outcome.status, 0) + 1
        svc.session.commit()
    return "research: " + ", ".join(f"{k}={v}" for k, v in outcomes.items())


def generate_job(svc: Services, limit: int = 10) -> str:
    leads = LeadRepository(svc.session).needing_initial_email(limit)
    if not leads:
        return "generate: nothing to do"
    created = 0
    for lead in leads:
        if lead.researched_at is None:
            svc.researcher.research(lead)  # research first (cached forever afterwards)
        if svc.generator.generate_initial(lead):
            created += 1
        svc.session.commit()
    return f"generate: {created} draft(s) created"


def followup_job(svc: Services) -> str:
    result = svc.followups.process_due()
    return (f"followups: generated={len(result.generated)} completed={len(result.completed)} "
            f"skipped={len(result.skipped)}")


def send_job(svc: Services, wait: bool = True, max_messages: int | None = None) -> str:
    return f"send: {svc.sender.process_queue(max_messages=max_messages, wait=wait)}"


def inbox_job(svc: Services) -> str:
    return f"inbox: {svc.inbox.check()}"


JOBS: dict[str, Callable[..., str]] = {
    "inbox": inbox_job,          # detect replies first so we never follow up on someone who answered
    "followups": followup_job,
    "research": research_job,
    "generate": generate_job,
    "send": send_job,
}


def run_job(name: str, settings: Settings | None = None, **kwargs: object) -> str:
    """Run one job (or 'all') safely. Returns a human-readable summary."""
    settings = settings or get_settings()
    names = list(JOBS) if name == "all" else [name]
    if any(n not in JOBS for n in names):
        return f"unknown job '{name}' (choose from: all, {', '.join(JOBS)})"
    engine = init_db(get_engine(settings))
    summaries: list[str] = []
    for job_name in names:
        try:
            with job_lock(job_name, settings), session_scope(engine) as session:
                svc = Services(session, settings)
                job_kwargs = {k: v for k, v in kwargs.items()
                              if k in JOBS[job_name].__code__.co_varnames}
                summary = JOBS[job_name](svc, **job_kwargs)
        except JobLockedError as exc:
            summary = f"{job_name}: skipped ({exc})"
        except GmailAuthError as exc:
            summary = f"{job_name}: Gmail authorisation required - {exc}"
            log_event("error", summary, level=40, job=job_name)
        except (GmailError, AIError) as exc:
            summary = f"{job_name}: failed - {exc}"
            log_event("error", summary, level=40, job=job_name)
        except Exception as exc:  # the scheduler must survive anything
            summary = f"{job_name}: unexpected error - {type(exc).__name__}: {exc}"
            logger.exception("Job %s crashed", job_name)
        summaries.append(summary)
        log_event("job_finished", summary, job=job_name)
    return "\n".join(summaries)


# --------------------------------------------------------------------------- dev scheduler
@dataclass
class ScheduleEntry:
    job: str
    every_minutes: int
    next_run: float = 0.0


DEFAULT_SCHEDULE = [
    ScheduleEntry("inbox", 15),
    ScheduleEntry("followups", 60),
    ScheduleEntry("research", 30),
    ScheduleEntry("generate", 30),
    ScheduleEntry("send", 5),
]


def run_scheduler(schedule: list[ScheduleEntry] | None = None, settings: Settings | None = None,
                  max_iterations: int | None = None, tick_seconds: float = 20.0) -> None:
    """Simple blocking loop - good enough for development and demos."""
    schedule = schedule or [ScheduleEntry(e.job, e.every_minutes) for e in DEFAULT_SCHEDULE]
    settings = settings or get_settings()
    logger.info("Scheduler started: %s", ", ".join(f"{e.job}/{e.every_minutes}m" for e in schedule))
    iterations = 0
    try:
        while max_iterations is None or iterations < max_iterations:
            now = time.monotonic()
            for entry in schedule:
                if now >= entry.next_run:
                    logger.info(run_job(entry.job, settings, wait=True))
                    entry.next_run = time.monotonic() + entry.every_minutes * 60
            iterations += 1
            if max_iterations is None or iterations < max_iterations:
                time.sleep(tick_seconds)
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")
