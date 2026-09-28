# Architecture & Design Notes

## Guiding principles

1. **€0/month** – SQLite, free-tier LLMs (Gemini / Groq) called over plain REST with `httpx`,
   the official Gmail API, Streamlit locally. No paid SaaS.
2. **AI only where language matters** – research summarisation, email writing, reply
   classification and reply drafting. Everything else (dates, counters, limits, dedupe,
   status transitions, scheduling, bounce/auto-reply detection) is deterministic Python.
3. **Safe by default** – `DEMO_MODE=true` (fake mailbox) and `SAFE_MODE=true` (human approval),
   conservative sending limits, suppression list checked immediately before every send.
4. **Layered & testable** – services receive a DB session, settings, an `AIService` and a
   `GmailClient`, so tests inject fakes for everything external.

## Layers

| Layer | Package | Responsibility |
|-------|---------|----------------|
| Config | `app/config` | typed settings (`pydantic-settings`), JSON logging with secret redaction |
| Data | `app/database` | SQLAlchemy 2.0 models, engine/session, repositories (queries) |
| AI | `app/ai` | `AIProvider` interface, Gemini/Groq/Mock providers, `AIService` (cache, fallback, usage log), compact prompts |
| Email | `app/email` | Gmail OAuth client, fake demo mailbox, MIME building, thread helpers, sender with limits, inbox monitor |
| Leads | `app/leads` | CSV/manual/pasted import, validation, deduplication, status machine, company research |
| Outreach | `app/outreach` | campaigns & ICP, email generation, reply classification & rules, follow-ups, approval queue |
| Jobs | `app/scheduler` | idempotent job functions + a lightweight loop scheduler |
| API | `app/main.py` | optional FastAPI layer (health, stats, job triggers) |
| UI | `dashboard/` | Streamlit dashboard |

## Database design

```mermaid
erDiagram
    CAMPAIGN ||--o{ LEAD : has
    LEAD ||--o{ EMAIL_MESSAGE : receives
    LEAD ||--o{ REPLY : sends
    REPLY |o--o| EMAIL_MESSAGE : "suggested reply"
    CAMPAIGN {
        int id PK
        string name UK
        string status "DRAFT|ACTIVE|PAUSED|STOPPED|COMPLETED"
        bool auto_send
        int max_emails_per_day
        int max_followups
        text icp_json "AI-generated ideal customer profile (cached)"
    }
    LEAD {
        int id PK
        int campaign_id FK
        string email_normalized "unique per campaign"
        string domain "indexed, dedupe"
        string status "NEW..COMPLETED"
        datetime next_followup_at "indexed"
        int followup_count
        string gmail_thread_id "indexed"
        bool do_not_contact
        bool human_review_required
        text research_json "cached AI research"
    }
    EMAIL_MESSAGE {
        int id PK
        int lead_id FK
        string kind "INITIAL|FOLLOWUP|REPLY|REFERRAL_REQUEST"
        string status "PENDING_APPROVAL|APPROVED|SENDING|SENT|REJECTED|FAILED|CANCELLED"
        string approved_by "human|auto"
        string gmail_message_id
        string gmail_thread_id
    }
    REPLY {
        int id PK
        int lead_id FK
        string gmail_message_id UK
        string category
        float confidence
        bool requires_human
    }
    SUPPRESSION_ENTRY { int id PK
        string email UK
        string domain
        string reason }
    PROCESSED_MESSAGE { string gmail_message_id PK
        string outcome }
    APP_SETTING { string key PK
        text value }
    AI_CACHE_ENTRY { string key PK
        text response }
    AI_USAGE { int id PK
        string provider
        string purpose
        int prompt_tokens
        int completion_tokens
        bool cached }
```

Key invariants:

* **No double sends** – a message is *claimed* with an atomic
  `UPDATE ... SET status='SENDING' WHERE id=? AND status='APPROVED'` before the Gmail call.
* **No double processing** – every Gmail message id handled by the inbox monitor is stored
  in `processed_messages` in the same transaction as the resulting `Reply`.
* **Suppression is global** – `suppression_entries` is checked right before each send,
  independent of lead status.
* **Daily limit** is derived from `email_messages.sent_at` (source of truth, cannot drift).

## Lead lifecycle

```mermaid
stateDiagram-v2
    [*] --> NEW
    NEW --> RESEARCHED
    NEW --> READY
    RESEARCHED --> READY: email drafted
    READY --> EMAIL_SENT: approved + sent
    EMAIL_SENT --> FOLLOWUP_DUE: no reply after N days
    FOLLOWUP_DUE --> EMAIL_SENT: follow-up sent
    EMAIL_SENT --> REPLIED
    REPLIED --> INTERESTED
    REPLIED --> NOT_INTERESTED
    REPLIED --> WRONG_PERSON
    REPLIED --> OUT_OF_OFFICE
    REPLIED --> HUMAN_REVIEW
    OUT_OF_OFFICE --> FOLLOWUP_DUE: after return date
    EMAIL_SENT --> BOUNCED
    INTERESTED --> COMPLETED
    NOT_INTERESTED --> COMPLETED
    BOUNCED --> COMPLETED
```

## Implementation roadmap

| Phase | Deliverable | Verification |
|------:|-------------|--------------|
| 1 | Project skeleton, settings, logging, utils | import check |
| 2 | SQLite models, sessions, repositories, status machine | model tests |
| 3 | CSV / manual / pasted import, validation, dedupe | import & dedupe tests |
| 4 | AI provider abstraction (Gemini, Groq, Mock), cache, fallback | provider tests with mocked HTTP |
| 5 | Company research (SSRF-safe fetch, text extraction, AI summary) | research tests with mocked HTTP |
| 6 | Email generation + quality checks + approval queue | generator tests |
| 7 | Gmail OAuth client, demo mailbox, sender with limits | sender/limit tests |
| 8 | Inbox monitor, reply classification, response rules | classifier/rules tests |
| 9 | Follow-up scheduling | follow-up tests |
| 10 | Streamlit dashboard | manual run + smoke import |
| 11 | Full test suite | `pytest` |
| 12 | README, cleanup, final validation | checklist |
