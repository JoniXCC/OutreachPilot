"""Streamlit dashboard for the AI Sales Outreach Agent.

Run with:  python run.py dashboard   (or: streamlit run dashboard/streamlit_app.py)
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from app.ai.provider import AIError  # noqa: E402
from app.config.logging_config import read_log_tail, setup_logging  # noqa: E402
from app.config.settings import get_settings  # noqa: E402
from app.database.database import get_engine, init_db, make_session_factory  # noqa: E402
from app.database.models import (  # noqa: E402
    AIUsage, Campaign, CampaignStatus, EmailMessage, Lead, LeadStatus, MessageKind, MessageStatus,
)
from app.database.repositories import (  # noqa: E402
    AppSettingRepository, CampaignRepository, LeadRepository, MessageRepository, ReplyRepository,
    SuppressionRepository,
)
from app.email.gmail_client import GmailAuthError, GmailError  # noqa: E402
from app.leads.importer import ImportFormatError, LeadInput  # noqa: E402
from app.leads.lead_service import leads_to_csv  # noqa: E402
from app.outreach.approval_service import ApprovalError  # noqa: E402
from app.outreach.campaign_service import CampaignInput  # noqa: E402
from app.outreach.status_machine import LEAD_TRANSITIONS, InvalidTransitionError  # noqa: E402
from app.services import Services  # noqa: E402
from app.utils.validators import is_valid_email  # noqa: E402
from dashboard import charts  # noqa: E402

st.set_page_config(page_title="OutreachPilot", page_icon="📬", layout="wide")

settings = get_settings()
setup_logging(settings.log_level, settings.resolve_path(settings.log_file))

EXPECTED_ERRORS = (ApprovalError, InvalidTransitionError, ValueError, AIError, GmailError,
                   ImportFormatError, ValidationError)


@st.cache_resource
def _session_factory(database_url: str):  # keyed on the URL so a config change gets a new engine
    return make_session_factory(init_db(get_engine(settings)))


session = _session_factory(settings.resolved_database_url())()
svc = Services(session, settings)


# --------------------------------------------------------------------------- helpers
def flash(message: str, kind: str = "success") -> None:
    st.session_state["_flash"] = (kind, message)


def show_flash() -> None:
    item = st.session_state.pop("_flash", None)
    if item:
        getattr(st, item[0])(item[1])


def act(fn: Callable[[], Any], success: str | Callable[[Any], str]) -> None:
    """Run a mutating action, show the outcome after a rerun, never crash the page."""
    try:
        result = fn()
        session.commit()
    except GmailAuthError as exc:
        session.rollback()
        st.error(f"Gmail authorisation needed: {exc}")
        return
    except EXPECTED_ERRORS as exc:
        session.rollback()
        st.error(str(exc))
        return
    flash(success(result) if callable(success) else success)
    st.rerun()


def fmt_dt(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def campaign_filter() -> Campaign | None:
    campaigns = CampaignRepository(session).list()
    options = ["All campaigns"] + [f"{c.id} · {c.name}" for c in campaigns]
    choice = st.sidebar.selectbox("Campaign", options, key="campaign_filter")
    if choice == "All campaigns":
        return None
    return CampaignRepository(session).get(int(choice.split(" · ")[0]))


def show_chart(chart, empty_text: str = "No data yet.") -> None:
    if chart is None:
        st.caption(empty_text)
    else:
        height = chart.to_dict().get("height", 240)
        st.altair_chart(chart, width="stretch", height=int(height) + 110)  # room for axis + legend


def mode_badges() -> None:
    parts = [
        ":orange-badge[DEMO MODE - no real email]" if settings.demo_mode else ":red-badge[LIVE GMAIL]",
        ":green-badge[SAFE MODE - human approval]" if settings.safe_mode else ":orange-badge[AUTOMATIC MODE allowed]",
        f":blue-badge[AI: {svc.ai.provider_label}]",
    ]
    if AppSettingRepository(session).get_bool(AppSettingRepository.SENDING_PAUSED):
        parts.append(":red-badge[SENDING PAUSED]")
    st.markdown(" ".join(parts))


# --------------------------------------------------------------------------- pages
def page_overview(campaign: Campaign | None) -> None:
    st.title("Overview")
    mode_badges()
    stats = svc.campaigns.stats(campaign.id if campaign else None)
    cols = st.columns(7)
    cols[0].metric("Total leads", stats["total_leads"])
    cols[1].metric("Emails sent", stats["emails_sent"])
    cols[2].metric("Replies", stats["replied_leads"])
    cols[3].metric("Interested", stats["interested"])
    cols[4].metric("Reply rate", f"{stats['reply_rate']:.0%}")
    cols[5].metric("Pending approvals", stats["pending_approvals"])
    cols[6].metric("Follow-ups due", stats["followups_due"])

    left, right = st.columns(2)
    with left:
        st.subheader("Lead pipeline")
        order = [s.value for s in LeadStatus if s.value in stats["status_counts"]]
        show_chart(charts.hbar(stats["status_counts"], "Status", "Leads", order=order))
    with right:
        st.subheader("Reply categories")
        show_chart(charts.hbar(stats["reply_categories"], "Category", "Replies"), "No replies yet.")

    st.subheader("Run pipeline steps")
    st.caption("Each step is idempotent and only calls the AI when there is real work to do. "
               "The same jobs run on a schedule via `python run.py scheduler` or Windows Task Scheduler.")
    c = st.columns(5)
    if c[0].button("1 · Research new leads", width="stretch"):
        from app.scheduler.jobs import research_job
        act(lambda: research_job(svc), lambda r: r)
    if c[1].button("2 · Draft emails", width="stretch"):
        from app.scheduler.jobs import generate_job
        act(lambda: generate_job(svc), lambda r: r + " - review them in Email Queue")
    if c[2].button("3 · Send approved", width="stretch"):
        act(lambda: svc.sender.process_queue(wait=False), lambda r: f"Send run: {r}")
    if c[3].button("4 · Check inbox", width="stretch"):
        act(lambda: svc.inbox.check(), lambda r: f"Inbox: {r}")
    if c[4].button("5 · Process follow-ups", width="stretch"):
        from app.scheduler.jobs import followup_job
        act(lambda: followup_job(svc), lambda r: r)

    if settings.demo_mode:
        st.subheader("Demo")
        st.caption("Creates a fictional campaign on reserved `.example` domains, drafts, approves and "
                   "'sends' to the local demo mailbox, then simulates replies. Nobody is emailed.")
        d1, d2 = st.columns(2)
        if d1.button("Run complete demo", type="primary", width="stretch"):
            from app.demo import run_demo_flow
            act(lambda: run_demo_flow(session, settings),
                lambda r: f"Demo done: {r.sent} emails sent to the demo mailbox, {r.replies} replies simulated.")
        if d2.button("Reset demo data", width="stretch"):
            from app.demo import reset_demo
            act(lambda: reset_demo(session, settings), "Demo data removed.")


def campaign_form(existing: Campaign | None) -> CampaignInput | None:
    e = existing
    with st.form(f"campaign_form_{e.id if e else 'new'}"):
        c1, c2 = st.columns(2)
        name = c1.text_input("Campaign name *", e.name if e else "")
        company = c2.text_input("Your company name *", e.company_name if e else "")
        product = c1.text_input("Product / service name *", e.product_name if e else "")
        sender = c2.text_input("Sender name (signature)", e.sender_name if e else settings.sender_name)
        description = st.text_area("Product description", e.product_description if e else "", height=80)
        value_prop = st.text_area("Value proposition", e.value_proposition if e else "", height=70)
        c3, c4, c5 = st.columns(3)
        industry = c3.text_input("Target industry", e.target_industry if e else "")
        size = c4.text_input("Target company size", e.target_company_size if e else "")
        locations = c5.text_input("Target locations", e.target_locations if e else "")
        roles = c3.text_input("Desired job titles", e.target_roles if e else "")
        tone = c4.text_input("Tone of voice", e.tone if e else "friendly, professional, concise")
        cta = c5.text_input("Call to action", e.call_to_action if e else "Would you be open to a 10-minute demo?")
        c6, c7, c8, c9 = st.columns(4)
        max_day = c6.number_input("Max emails / day", 1, settings.max_emails_per_day,
                                  min(e.max_emails_per_day, settings.max_emails_per_day) if e else settings.max_emails_per_day)
        max_f = c7.number_input("Max follow-ups", 0, settings.max_followups,
                                min(e.max_followups, settings.max_followups) if e else settings.max_followups)
        f1 = c8.number_input("Follow-up 1 after (days)", 1, 60, e.followup_1_days if e else settings.followup_1_days)
        f2 = c9.number_input("Follow-up 2 after (days)", 1, 60, e.followup_2_days if e else settings.followup_2_days)
        auto = st.checkbox(
            "Automatic mode: send without manual approval (only effective when SAFE_MODE=false; "
            "emails with quality warnings and replies to leads always need approval)",
            e.auto_send if e else False)
        submitted = st.form_submit_button("Save campaign", type="primary")
    if not submitted:
        return None
    try:
        return CampaignInput(name=name, company_name=company, sender_name=sender, product_name=product,
                             product_description=description, value_proposition=value_prop,
                             target_industry=industry, target_company_size=size, target_locations=locations,
                             target_roles=roles, tone=tone, call_to_action=cta, max_emails_per_day=int(max_day),
                             max_followups=int(max_f), followup_1_days=int(f1), followup_2_days=int(f2),
                             auto_send=auto)
    except ValidationError as exc:
        st.error("Please fix: " + "; ".join(f"{err['loc'][0]}: {err['msg']}" for err in exc.errors()))
        return None


def page_campaigns(campaign: Campaign | None) -> None:
    st.title("Campaigns")
    campaigns = CampaignRepository(session).list()
    tab_edit, tab_new = st.tabs(["Manage", "Create new"])
    with tab_new:
        data = campaign_form(None)
        if data:
            act(lambda: svc.campaigns.create(data), f"Campaign '{data.name}' created (status DRAFT).")
    with tab_edit:
        if not campaigns:
            st.info("No campaigns yet - create one or run the demo from Overview.")
            return
        names = [f"{c.id} · {c.name}" for c in campaigns]
        default = names.index(f"{campaign.id} · {campaign.name}") if campaign else 0
        chosen = CampaignRepository(session).get(int(st.selectbox("Campaign", names, index=default).split(" · ")[0]))
        st.markdown(f"**Status:** `{chosen.status}` · **Leads:** {len(chosen.leads)} · "
                    f"**Auto-send:** {'on' if chosen.auto_send else 'off'}")
        b = st.columns(5)
        transitions = {"Activate": CampaignStatus.ACTIVE, "Pause": CampaignStatus.PAUSED,
                       "Stop (cancel queue)": CampaignStatus.STOPPED, "Complete": CampaignStatus.COMPLETED,
                       "Back to draft": CampaignStatus.DRAFT}
        for col, (label, status) in zip(b, transitions.items()):
            if col.button(label, key=f"cs_{status}", width="stretch"):
                act(lambda s=status: svc.campaigns.set_status(chosen, s), f"Campaign is now {status}.")

        st.subheader("Ideal customer profile")
        icp = chosen.icp
        if icp:
            st.write(icp.get("summary", ""))
            i1, i2, i3 = st.columns(3)
            i1.markdown("**Pain points**\n" + "\n".join(f"- {p}" for p in icp.get("pain_points", [])))
            i2.markdown("**Good-fit signals**\n" + "\n".join(f"- {p}" for p in icp.get("qualifying_signals", [])))
            i3.markdown("**Disqualifiers**\n" + "\n".join(f"- {p}" for p in icp.get("disqualifiers", [])))
        if st.button("Regenerate ICP" if icp else "Define ICP with AI (1 call, cached)"):
            act(lambda: svc.campaigns.generate_icp(chosen, svc.ai, force=bool(icp)), "ICP updated.")

        st.subheader("Edit")
        data = campaign_form(chosen)
        if data:
            act(lambda: svc.campaigns.update(chosen, data), "Campaign saved.")
        with st.expander("Danger zone"):
            confirm = st.checkbox(f"I understand this deletes '{chosen.name}' and all its leads and emails")
            if st.button("Delete campaign", disabled=not confirm):
                act(lambda: svc.campaigns.delete(chosen), "Campaign deleted.")


def lead_rows(leads: list[Lead]) -> pd.DataFrame:
    return pd.DataFrame([{
        "ID": lead.id, "Company": lead.company_name, "Contact": lead.contact_name, "Role": lead.contact_role,
        "Email": lead.email, "Status": lead.status, "Last Contact": fmt_dt(lead.last_contacted_at),
        "Next Follow-up": fmt_dt(lead.next_followup_at), "Followups": lead.followup_count,
        "Reply Category": lead.reply_status or "", "Review": "⚑" if lead.human_review_required else "",
        "DNC": "⛔" if lead.do_not_contact else "",
    } for lead in leads])


def page_leads(campaign: Campaign | None) -> None:
    st.title("Leads")
    tab_table, tab_import, tab_detail = st.tabs(["Table", "Import", "Lead detail"])
    with tab_table:
        f1, f2, f3 = st.columns([2, 2, 1])
        statuses = f1.multiselect("Status", [s.value for s in LeadStatus])
        search = f2.text_input("Search company, contact or email")
        review_only = f3.checkbox("Needs review only")
        leads = list(LeadRepository(session).list(campaign.id if campaign else None, statuses or None,
                                                  search or None, True if review_only else None))
        if leads:
            st.dataframe(lead_rows(leads), hide_index=True, width="stretch")
            st.download_button("Export CSV (open in Excel / Google Sheets)", leads_to_csv(leads),
                               "leads.csv", "text/csv")
        else:
            st.info("No leads match.")

    with tab_import:
        if campaign is None:
            st.info("Select a campaign in the sidebar to import leads into it.")
        else:
            st.caption(f"Importing into **{campaign.name}**. Duplicates (email, company domain, or company + "
                       "contact) and suppressed addresses are skipped; invalid rows are reported.")
            m1, m2, m3 = st.tabs(["CSV upload", "Paste list", "Manual"])
            with m1:
                st.caption("Columns: company_name, website, contact_name, contact_role, email, industry, location")
                upload = st.file_uploader("CSV file", type=["csv", "txt"])
                if upload and st.button("Import CSV", type="primary"):
                    import_report(lambda: svc.leads.import_csv(campaign, upload.getvalue()))
            with m2:
                text = st.text_area("One lead per line", height=160, placeholder=(
                    "sarah@abc-dental.de\nTom Meyer <tom@meyer-physio.de>, Meyer Physio\n"
                    "Lisa Braun, Owner, lisa@braun-dental.de, Braun Dental"))
                if st.button("Import pasted list", type="primary") and text.strip():
                    import_report(lambda: svc.leads.import_pasted(campaign, text))
            with m3:
                with st.form("manual_lead", clear_on_submit=True):
                    c1, c2 = st.columns(2)
                    email = c1.text_input("Email *")
                    company = c2.text_input("Company")
                    contact = c1.text_input("Contact name")
                    role = c2.text_input("Role")
                    website = c1.text_input("Website")
                    location = c2.text_input("Location")
                    if st.form_submit_button("Add lead", type="primary"):
                        if not is_valid_email(email):
                            st.error("Please enter a valid email address.")
                        else:
                            import_report(lambda: svc.leads.import_rows(campaign, [LeadInput(
                                email=email, company_name=company, contact_name=contact, contact_role=role,
                                website=website, location=location)], "manual"))
            show_import_report()

    with tab_detail:
        lead_detail(campaign)


def import_report(fn: Callable[[], Any]) -> None:
    try:
        report = fn()
    except ImportFormatError as exc:
        st.error(f"Could not read the file: {exc}")
        return
    st.session_state["_import_report"] = report
    flash(report.summary, "success" if report.imported else "warning")
    st.rerun()


def show_import_report() -> None:
    report = st.session_state.pop("_import_report", None)
    if report and (report.invalid or report.duplicates):
        rows = [{"Row": i.row_number, "Email": i.email, "Problem": i.reason, "Type": "invalid"} for i in report.invalid]
        rows += [{"Row": i.row_number, "Email": i.email, "Problem": i.reason, "Type": "duplicate"}
                 for i in report.duplicates]
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def lead_detail(campaign: Campaign | None) -> None:
    leads = list(LeadRepository(session).list(campaign.id if campaign else None))
    if not leads:
        st.info("No leads yet.")
        return
    labels = {f"{lead.id} · {lead.company_name} · {lead.contact_name or lead.email}": lead for lead in leads}
    lead = labels[st.selectbox("Lead", list(labels))]
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**{lead.company_name}**  \n{lead.contact_name} · {lead.contact_role}  \n`{lead.email}`")
    c2.markdown(f"**Status:** `{lead.status}`  \n**Reply:** {lead.reply_status or '-'}  \n"
                f"**Follow-ups sent:** {lead.followup_count}")
    c3.markdown(f"**Website:** {lead.website or '-'}  \n**Next follow-up:** {fmt_dt(lead.next_followup_at) or '-'}  \n"
                f"**Do not contact:** {'yes' if lead.do_not_contact else 'no'}")

    st.markdown("**Research**")
    if lead.researched_at:
        research = lead.research
        st.write(research.get("company_summary") or "-")
        if research.get("relevant_signal"):
            st.write(f"Signal: {research['relevant_signal']}")
        st.caption(f"Confidence {research.get('confidence', 0):.2f} · researched {fmt_dt(lead.researched_at)}"
                   + (f" · note: {lead.research_error}" if lead.research_error else ""))
    else:
        st.caption("Not researched yet.")

    a = st.columns(4)
    if a[0].button("Research now" if not lead.researched_at else "Re-research", width="stretch"):
        act(lambda: svc.researcher.research(lead, force=bool(lead.researched_at)), lambda r: f"Research: {r.status} {r.detail}")
    if a[1].button("Draft email", width="stretch"):
        act(lambda: svc.generator.generate_initial(lead),
            lambda m: "Draft ready in Email Queue." if m else "Lead cannot be contacted (see status / suppression).")
    if not lead.do_not_contact and a[2].button("Do not contact", width="stretch"):
        act(lambda: svc.leads.mark_do_not_contact(lead), "Lead suppressed permanently.")
    allowed = sorted(s.value for s in LEAD_TRANSITIONS[LeadStatus(lead.status)])
    if allowed:
        with a[3].popover("Change status", width="stretch"):
            new_status = st.selectbox("New status", allowed, key=f"ns_{lead.id}")
            if st.button("Apply", key=f"apply_{lead.id}"):
                act(lambda: svc.leads.set_status(lead, new_status, clear_review=True),
                    f"Status set to {new_status}.")

    st.markdown("**Conversation**")
    events = [(m.sent_at or m.created_at, "→ us", m) for m in lead.messages] + \
             [(r.received_at, "← them", r) for r in lead.replies]
    for when, direction, item in sorted(events, key=lambda e: e[0]):
        if isinstance(item, EmailMessage):
            title = f"{direction} {item.kind} · {item.status} · {fmt_dt(when)} · {item.subject}"
        else:
            title = f"{direction} REPLY · {item.category} ({(item.confidence or 0):.2f}) · {fmt_dt(when)}"
        with st.expander(title):
            if isinstance(item, EmailMessage):
                st.text(item.body)
            else:
                st.text(item.body)
                st.caption(f"AI summary: {item.summary} · action: {item.action_taken}")


def page_queue(campaign: Campaign | None) -> None:
    st.title("Email Queue")
    repo = MessageRepository(session)
    cid = campaign.id if campaign else None
    pending = list(repo.pending_approval(cid))
    approved = list(repo.by_status([MessageStatus.APPROVED], cid))

    s1, s2, s3 = st.columns(3)
    s1.metric("Awaiting approval", len(pending))
    s2.metric("Approved, waiting to send", len(approved))
    s3.metric("Sends left today (global)", svc.sender.remaining_today())
    if approved:
        wait = svc.sender.seconds_until_next_send()
        label = "Send next approved email" if wait == 0 else f"Next send allowed in {int(wait)}s"
        if st.button(label, type="primary", disabled=wait > 0):
            act(lambda: svc.sender.process_queue(wait=False), lambda r: f"Send run: {r}")
        st.caption(f"Pacing: {settings.min_seconds_between_emails}s between emails, max "
                   f"{settings.max_emails_per_day}/day. The scheduled `send` job sends the rest automatically.")

    if not pending:
        st.success("Nothing awaiting approval.")
    for msg in pending:
        lead = msg.lead
        with st.container(border=True):
            h1, h2 = st.columns([3, 2])
            h1.markdown(f"**To:** {lead.contact_name or ''} `<{msg.to_email}>` · **{lead.company_name}**")
            h2.markdown(f"`{msg.kind}` · {'AI' if msg.ai_generated else 'template'}"
                        + "".join(f" :orange-badge[{f}]" for f in msg.quality_flags.split(",") if f))
            if msg.kind in (MessageKind.REPLY, MessageKind.REFERRAL_REQUEST) and lead.replies:
                last = lead.replies[-1]
                st.info(f"**Their message ({last.category}):** {last.body}")
            st.caption(f"Personalisation: {msg.personalization_reason or '-'}")
            subject = st.text_input("Subject", msg.subject, key=f"subj_{msg.id}")
            body = st.text_area("Body", msg.body, height=260, key=f"body_{msg.id}")
            b = st.columns(4)
            if b[0].button("Approve", key=f"ap_{msg.id}", type="primary", width="stretch"):
                act(lambda m=msg, s=subject, t=body: svc.approvals.approve(m, s, t), "Approved - it will be sent within the limits.")
            if b[1].button("Save edit", key=f"ed_{msg.id}", width="stretch"):
                act(lambda m=msg, s=subject, t=body: svc.approvals.edit(m, s, t), "Edit saved.")
            if msg.kind == MessageKind.INITIAL and b[2].button("Regenerate", key=f"rg_{msg.id}", width="stretch"):
                act(lambda m=msg: svc.approvals.regenerate(m, svc.generator), "New version generated.")
            if b[3].button("Reject", key=f"rj_{msg.id}", width="stretch"):
                act(lambda m=msg: svc.approvals.reject(m), "Rejected.")

    with st.expander(f"Approved queue ({len(approved)})"):
        for msg in approved:
            st.markdown(f"- `{msg.kind}` to `{msg.to_email}` - {msg.subject} (approved by {msg.approved_by})")
    with st.expander("Recently sent"):
        sent = list(repo.by_status([MessageStatus.SENT], cid))[-50:]
        if sent:
            st.dataframe(pd.DataFrame([{"Sent": fmt_dt(m.sent_at), "Kind": m.kind, "To": m.to_email,
                                        "Subject": m.subject} for m in reversed(sent)]),
                         hide_index=True, width="stretch")


def page_inbox(campaign: Campaign | None) -> None:
    st.title("Inbox & Replies")
    last = AppSettingRepository(session).get(AppSettingRepository.LAST_INBOX_CHECK)
    st.caption(f"Last inbox check: {last[:16].replace('T', ' ') if last else 'never'} (UTC). "
               "Only threads started by this app are read; no AI call happens when there's nothing new.")
    if st.button("Check inbox now", type="primary"):
        act(lambda: svc.inbox.check(), lambda r: f"Inbox: {r}")

    if settings.demo_mode:
        with st.expander("Demo: simulate a reply from a lead", expanded=False):
            from app.email.demo_mailbox import SCENARIOS
            threads = [lead for lead in LeadRepository(session).list(campaign.id if campaign else None)
                       if lead.gmail_thread_id]
            if not threads:
                st.caption("Send at least one email first.")
            else:
                options = {f"{lead.company_name} · {lead.email}": lead for lead in threads}
                c1, c2 = st.columns(2)
                target = options[c1.selectbox("Lead", list(options))]
                scenario = c2.selectbox("Reply type", list(SCENARIOS))
                if st.button("Simulate reply and check inbox"):
                    def _simulate():
                        svc.gmail.simulate_scenario(target.gmail_thread_id, target.email, scenario)
                        return svc.inbox.check()
                    act(_simulate, lambda r: f"Simulated '{scenario}'. Inbox: {r}")

    replies = list(ReplyRepository(session).list(campaign.id if campaign else None))
    if not replies:
        st.info("No replies yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Received": fmt_dt(r.received_at), "Company": r.lead.company_name, "From": r.from_email,
        "Category": r.category, "Confidence": round(r.confidence or 0, 2), "By": r.classified_by,
        "Summary": r.summary, "Action": r.action_taken,
    } for r in replies]), hide_index=True, width="stretch",
        column_config={"Confidence": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.2f")})
    for r in replies[:20]:
        with st.expander(f"{r.lead.company_name} · {r.category} · {fmt_dt(r.received_at)}"):
            st.text(r.body)


def page_attention(campaign: Campaign | None) -> None:
    st.title("Needs human attention")
    st.caption("Interested leads, questions, unclear or low-confidence replies. The system never tries to "
               "close a sale on its own - suggested replies wait in the Email Queue.")
    leads = list(LeadRepository(session).list(campaign.id if campaign else None, human_review=True))
    if not leads:
        st.success("Nothing needs your attention right now.")
        return
    for lead in sorted(leads, key=lambda x: (x.status != LeadStatus.INTERESTED, x.id)):
        with st.container(border=True):
            badge = ":green-badge[INTERESTED]" if lead.status == LeadStatus.INTERESTED else f"`{lead.status}`"
            st.markdown(f"### {lead.company_name} {badge}")
            st.markdown(f"{lead.contact_name} · {lead.contact_role} · `{lead.email}`")
            if lead.replies:
                last = lead.replies[-1]
                st.info(f"**Latest reply** ({last.category}, confidence {(last.confidence or 0):.2f}): {last.body}")
                st.caption(f"AI summary: {last.summary}")
            drafts = [m for m in lead.messages if m.status == MessageStatus.PENDING_APPROVAL]
            if drafts:
                st.markdown(f"📝 {len(drafts)} suggested reply draft(s) waiting in **Email Queue**.")
            c1, c2, c3 = st.columns(3)
            if c1.button("Mark handled", key=f"h_{lead.id}", width="stretch"):
                def _handled(lead=lead):
                    lead.human_review_required = False
                act(_handled, "Marked as handled.")
            if c2.button("Close as completed", key=f"c_{lead.id}", width="stretch"):
                act(lambda lead=lead: svc.leads.set_status(lead, LeadStatus.COMPLETED, clear_review=True),
                    "Lead completed.")
            if not lead.do_not_contact and c3.button("Do not contact", key=f"d_{lead.id}", width="stretch"):
                act(lambda lead=lead: svc.leads.mark_do_not_contact(lead), "Lead suppressed.")


def page_analytics(campaign: Campaign | None) -> None:
    st.title("Analytics")
    st.caption("Only measurable facts: no tracking pixels, no invented open rates. 'Delivered' = sent minus "
               "detected bounces.")
    stats = svc.campaigns.stats(campaign.id if campaign else None)
    row = st.columns(5)
    row[0].metric("Initial emails sent", stats["initial_sent"])
    row[1].metric("Delivered (est.)", stats["delivered_estimate"])
    row[2].metric("Follow-ups sent", stats["followups_sent"])
    row[3].metric("Reply rate", f"{stats['reply_rate']:.0%}")
    row[4].metric("Positive reply rate", f"{stats['positive_reply_rate']:.0%}")
    row = st.columns(5)
    row[0].metric("Replied", stats["replied_leads"])
    row[1].metric("Interested", stats["interested"])
    row[2].metric("Not interested / opt-out", stats["not_interested"])
    row[3].metric("Bounced", stats["bounced"])
    row[4].metric("In human review", stats["human_review"])

    stmt = select(func.date(EmailMessage.sent_at), func.count()).where(EmailMessage.status == MessageStatus.SENT)
    if campaign:
        stmt = stmt.where(EmailMessage.campaign_id == campaign.id)
    per_day = pd.DataFrame(session.execute(stmt.group_by(func.date(EmailMessage.sent_at))).all(),
                           columns=["Day", "Emails"])
    st.subheader("Emails sent per day")
    show_chart(charts.daily_bars(per_day, "Day", "Emails", "Emails sent"), "Nothing sent yet.")
    left, right = st.columns(2)
    with left:
        st.subheader("Reply outcomes")
        show_chart(charts.hbar(stats["reply_categories"], "Category", "Replies"), "No replies yet.")
    with right:
        st.subheader("Lead status")
        show_chart(charts.hbar(stats["status_counts"], "Status", "Leads"))


def page_compliance(_: Campaign | None) -> None:
    st.title("Compliance & controls")
    app_settings = AppSettingRepository(session)
    paused = app_settings.get_bool(AppSettingRepository.SENDING_PAUSED)
    st.subheader("Global kill switch")
    if st.toggle("Pause ALL sending", value=paused) != paused:
        act(lambda: app_settings.set(AppSettingRepository.SENDING_PAUSED, "false" if paused else "true"),
            "Sending resumed." if paused else "All sending paused.")
    st.caption(f"Limits: {settings.max_emails_per_day} emails/day · {settings.min_seconds_between_emails}s "
               f"between emails · max {settings.max_followups} follow-ups · SAFE_MODE={settings.safe_mode}")

    st.subheader("Suppression list")
    st.caption("Addresses here are never contacted again, in any campaign, until manually removed. "
               "Unsubscribe replies and bounces are added automatically.")
    repo = SuppressionRepository(session)
    with st.form("suppress", clear_on_submit=True):
        c1, c2, c3 = st.columns([3, 3, 1])
        value = c1.text_input("Email or domain (e.g. name@company.com or company.com)")
        note = c2.text_input("Note")
        if c3.form_submit_button("Add"):
            if "@" in value and is_valid_email(value):
                act(lambda: repo.add_email(value, note=note), f"{value} suppressed.")
            elif value and "." in value and "@" not in value:
                act(lambda: repo.add_domain(value, note=note), f"Domain {value} suppressed.")
            else:
                st.error("Enter a valid email address or domain.")
    entries = list(repo.list())
    if entries:
        st.dataframe(pd.DataFrame([{"ID": e.id, "Email": e.email or "", "Domain": e.domain or "",
                                    "Reason": e.reason, "Note": e.note, "Added": fmt_dt(e.created_at)}
                                   for e in entries]), hide_index=True, width="stretch")
        with st.expander("Remove an entry (manual reset)"):
            entry_id = st.selectbox("Entry", [e.id for e in entries],
                                    format_func=lambda i: next(f"{e.email or e.domain} ({e.reason})" for e in entries if e.id == i))
            if st.button("Remove from suppression list"):
                act(lambda: repo.remove(entry_id), "Entry removed.")


def page_logs(_: Campaign | None) -> None:
    st.title("Logs & AI usage")
    tab_events, tab_ai, tab_settings = st.tabs(["Event log", "AI usage", "Configuration"])
    with tab_events:
        records = read_log_tail(settings.resolve_path(settings.log_file), 1000)
        if not records:
            st.info("No log entries yet.")
        else:
            df = pd.DataFrame(records)
            events = sorted(df["event"].dropna().unique())
            c1, c2 = st.columns(2)
            chosen = c1.multiselect("Event", events)
            levels = c2.multiselect("Level", ["INFO", "WARNING", "ERROR"])
            if chosen:
                df = df[df["event"].isin(chosen)]
            if levels:
                df = df[df["level"].isin(levels)]
            front = [c for c in ["ts", "level", "event", "message"] if c in df.columns]
            st.dataframe(df[front + [c for c in df.columns if c not in front and c != "logger"]],
                         hide_index=True, width="stretch", height=480)
    with tab_ai:
        rows = session.execute(select(AIUsage.purpose, AIUsage.cached, func.count(),
                                      func.sum(AIUsage.prompt_tokens), func.sum(AIUsage.completion_tokens))
                               .group_by(AIUsage.purpose, AIUsage.cached)).all()
        if not rows:
            st.info("No AI calls yet.")
        else:
            df = pd.DataFrame(rows, columns=["Purpose", "Cached", "Calls", "Prompt tokens", "Output tokens"])
            df["Type"] = df["Cached"].map({True: "Cache hit (free)", False: "Live call"})
            m = st.columns(3)
            m[0].metric("Live AI calls", int(df.loc[~df["Cached"], "Calls"].sum()))
            m[1].metric("Cache hits", int(df.loc[df["Cached"], "Calls"].sum()))
            m[2].metric("Tokens used (approx.)", int(df["Prompt tokens"].sum() + df["Output tokens"].sum()))
            show_chart(charts.stacked_hbar(df, "Purpose", "Type", "Calls", ["Live call", "Cache hit (free)"]))
            st.dataframe(df.drop(columns=["Cached"]), hide_index=True, width="stretch")
    with tab_settings:
        st.caption("Secrets are never displayed - only whether they are configured.")
        view = settings.public_view()
        st.dataframe(pd.DataFrame({"Setting": list(view), "Value": [str(v) for v in view.values()]}),
                     hide_index=True, width="stretch", height=520)
        token = settings.resolve_path(settings.gmail_token_path)
        creds = settings.resolve_path(settings.gmail_credentials_path)
        st.markdown(f"**Gmail:** {'demo mailbox (no real email)' if settings.demo_mode else 'live'} · "
                    f"credentials.json {'found' if creds.exists() else 'missing'} · "
                    f"token {'present' if token.exists() else 'missing - run `python run.py gmail-auth`'}")


PAGES = {
    "📊 Overview": page_overview,
    "🎯 Campaigns": page_campaigns,
    "👥 Leads": page_leads,
    "✉️ Email Queue": page_queue,
    "📥 Inbox & Replies": page_inbox,
    "🙋 Needs Attention": page_attention,
    "📈 Analytics": page_analytics,
    "🛡️ Compliance": page_compliance,
    "🧾 Logs & Settings": page_logs,
}


# Deep links, e.g. http://localhost:8501/?page=queue
PAGE_SLUGS = ["overview", "campaigns", "leads", "queue", "inbox", "attention", "analytics",
              "compliance", "logs"]


def main() -> None:
    st.sidebar.title("📬 OutreachPilot")
    pending = len(MessageRepository(session).pending_approval())
    review = len(LeadRepository(session).list(human_review=True))
    labels = [f"{name} ({pending})" if name == "✉️ Email Queue" and pending else
              f"{name} ({review})" if name == "🙋 Needs Attention" and review else name for name in PAGES]
    slug = st.query_params.get("page", "overview")
    start = PAGE_SLUGS.index(slug) if slug in PAGE_SLUGS else 0
    choice = st.sidebar.radio("Navigate", labels, index=start, label_visibility="collapsed")
    page = PAGES[list(PAGES)[labels.index(choice)]]
    campaign = campaign_filter()
    st.sidebar.divider()
    st.sidebar.caption("Demo mode" if settings.demo_mode else "Live mode")
    show_flash()
    try:
        page(campaign)
    finally:
        session.close()


main()
