"""Command-line entry point.

    python run.py init-db                 create the SQLite database
    python run.py demo [--reset]          full simulated run with fictional companies (no real email)
    python run.py dashboard               start the Streamlit dashboard
    python run.py run-jobs --job all      run background jobs once (for Windows Task Scheduler)
    python run.py scheduler               run jobs forever on a simple schedule (development)
    python run.py gmail-auth              one-time Gmail OAuth consent (opens a browser)
    python run.py import-csv FILE --campaign NAME
    python run.py status                  print pipeline statistics
    python run.py api                     start the optional local REST API
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.config.logging_config import setup_logging  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.database.database import get_engine, init_db, session_scope  # noqa: E402


def cmd_init_db(_: argparse.Namespace) -> int:
    settings = get_settings()
    init_db(get_engine(settings))
    print(f"Database ready: {settings.sqlite_path or settings.database_url}")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from app.demo import reset_demo, run_demo_flow

    settings = get_settings()
    if not settings.demo_mode:
        print("Refusing to run the demo with DEMO_MODE=false (it would use your real Gmail).")
        return 1
    engine = init_db(get_engine(settings))
    with session_scope(engine) as session:
        if args.reset:
            reset_demo(session, settings)
        report = run_demo_flow(session, settings)
    s = report.summary
    print("\n=== Demo run complete (nothing was emailed; see data/demo_mailbox.json) ===")
    print(f"AI provider:        {report.ai_provider}")
    print(f"Drafts generated:   {report.drafts}")
    print(f"Emails 'sent':      {report.sent}")
    print(f"Replies simulated:  {report.replies}")
    print(f"Reply categories:   {s['reply_categories']}")
    print(f"Lead statuses:      {s['status_counts']}")
    print(f"Reply rate:         {s['reply_rate']:.0%}   positive: {s['positive_reply_rate']:.0%}")
    print(f"Pending approvals:  {s['pending_approvals']} (suggested replies waiting for you)")
    print("\nNext: python run.py dashboard")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    app_path = ROOT / "dashboard" / "streamlit_app.py"
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(app_path),
                            "--server.address", "localhost", "--server.port", str(args.port)])


def cmd_run_jobs(args: argparse.Namespace) -> int:
    from app.scheduler.jobs import run_job
    print(run_job(args.job, wait=not args.no_wait))
    return 0


def cmd_scheduler(_: argparse.Namespace) -> int:
    from app.scheduler.jobs import run_scheduler
    run_scheduler()
    return 0


def cmd_gmail_auth(_: argparse.Namespace) -> int:
    from app.email.gmail_client import GmailAuthError, GoogleGmailClient
    settings = get_settings()
    try:
        GoogleGmailClient.authorize(settings.resolve_path(settings.gmail_credentials_path),
                                    settings.resolve_path(settings.gmail_token_path), interactive=True)
        client = GoogleGmailClient(settings.resolve_path(settings.gmail_credentials_path),
                                   settings.resolve_path(settings.gmail_token_path))
        print(f"Gmail authorised for {client.profile_email()}. Token saved to {settings.gmail_token_path}.")
    except GmailAuthError as exc:
        print(f"Gmail authorisation failed: {exc}")
        return 1
    return 0


def cmd_import_csv(args: argparse.Namespace) -> int:
    from app.database.repositories import CampaignRepository
    from app.leads.importer import ImportFormatError
    from app.leads.lead_service import LeadService

    settings = get_settings()
    with session_scope(init_db(get_engine(settings))) as session:
        campaign = CampaignRepository(session).get_by_name(args.campaign)
        if not campaign:
            print(f"Campaign '{args.campaign}' not found")
            return 1
        try:
            report = LeadService(session).import_csv(campaign, Path(args.file).read_bytes())
        except (ImportFormatError, OSError) as exc:
            print(f"Import failed: {exc}")
            return 1
        print(report.summary)
        for issue in report.invalid + report.duplicates:
            print(f"  row {issue.row_number}: {issue.email or '-'}: {issue.reason}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    from app.outreach.campaign_service import CampaignService
    settings = get_settings()
    with session_scope(init_db(get_engine(settings))) as session:
        stats = CampaignService(session, settings).stats()
    for key, value in stats.items():
        print(f"{key:>22}: {value}")
    return 0


def cmd_api(args: argparse.Namespace) -> int:
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port, reload=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Sales Outreach Agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db").set_defaults(func=cmd_init_db)
    p = sub.add_parser("demo")
    p.add_argument("--reset", action="store_true", help="delete previous demo data first")
    p.set_defaults(func=cmd_demo)
    p = sub.add_parser("dashboard")
    p.add_argument("--port", type=int, default=8501)
    p.set_defaults(func=cmd_dashboard)
    p = sub.add_parser("run-jobs")
    p.add_argument("--job", default="all", help="all | inbox | followups | research | generate | send")
    p.add_argument("--no-wait", action="store_true", help="don't sleep for pacing between sends")
    p.set_defaults(func=cmd_run_jobs)
    sub.add_parser("scheduler").set_defaults(func=cmd_scheduler)
    sub.add_parser("gmail-auth").set_defaults(func=cmd_gmail_auth)
    p = sub.add_parser("import-csv")
    p.add_argument("file")
    p.add_argument("--campaign", required=True)
    p.set_defaults(func=cmd_import_csv)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p = sub.add_parser("api")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=cmd_api)

    args = parser.parse_args(argv)
    settings = get_settings()
    setup_logging(settings.log_level, settings.resolve_path(settings.log_file))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
