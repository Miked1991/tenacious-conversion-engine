# Tenacious Conversion Engine — Architecture Reference

## 1. System Overview

The conversion engine is a FastAPI application that automates B2B outbound sales for Tenacious (East African engineering staffing). It receives email and SMS webhooks, enriches company profiles with four data sources, composes segment-aware cold emails, manages multi-turn conversations, qualifies leads, books Cal.com discovery calls, syncs to HubSpot, and logs every step to Langfuse.

```
                        ┌────────────────────────────────────────────────┐
                        │  External event sources                        │
                        │  Resend (email) · Africa's Talking (SMS)       │
                        └─────────────────┬──────────────────────────────┘
                                          │ POST /webhooks/email
                                          │ POST /webhooks/sms
                                          ▼
                        ┌────────────────────────────────────────────────┐
                        │  agent/main.py  (FastAPI orchestrator)         │
                        │  Rate limiting · HMAC verification             │
                        │  Background task dispatcher                    │
                        └───┬───────────────────────────────────────┬───┘
                 first      │                                       │  reply
                 contact    ▼                                       ▼
          _run_full_pipeline()                     _run_reply_pipeline()
                    │                                       │
         ┌──────────┴──────────┐               ┌───────────┴──────────┐
         │                     │               │                      │
         ▼                     ▼               ▼                      ▼
  enrichment_pipeline   signals_research  conversation_handler  booking_handler
  competitor_gap        email_outreach    hubspot_sync          sms_handler
         │                     │               │                      │
         └──────────┬──────────┘               └──────────┬───────────┘
                    ▼                                      ▼
              agent/db.py (SQLite)              Langfuse (all spans)
              HubSpot CRM
```

---

## 2. File-by-File Reference

### `agent/main.py` — FastAPI Orchestrator
**Responsibility:** Single entry point for all traffic. Routes events, applies security, wraps background tasks, runs health checks.

**Key design decisions:**
- `_verify_resend_signature()` — Svix HMAC-SHA256 + 5-minute staleness window; skipped when `RESEND_WEBHOOK_SECRET` is not set
- `_verify_at_webhook()` — Africa's Talking username check in form payload
- `_check_simulate_auth()` — `hmac.compare_digest` on `X-Simulate-Token`; 403 if `SIMULATE_TOKEN` not configured
- `_run_bg(fn, *args, **kwargs)` — wraps every `background_tasks.add_task` call; catches and logs unhandled exceptions that would otherwise silently vanish in FastAPI background tasks
- `/simulate` uses `await asyncio.to_thread(_run_full_pipeline, ...)` to avoid blocking the event loop during the full pipeline (Playwright + LLM calls)
- `/health` probes `db.ping()`, credentials presence, and Cal.com reachability; returns 503 when any check is degraded

**Rate limits:** webhooks → 60/min per IP; `/simulate` → 10/min per IP (slowapi)

---

### `agent/enrichment_pipeline.py` — Company Enrichment
**Responsibility:** Given an email address, gather company intelligence from four sources concurrently, merge results, and classify a sales segment.

**Four signal sources (run in parallel via `ThreadPoolExecutor(max_workers=4)`):**

| Source | API / Method | Confidence | Env var needed |
|--------|-------------|------------|----------------|
| Crunchbase ODM | REST API v4 or local CSV fallback | 0.9 (API) / 0.85 (CSV) | `CRUNCHBASE_API_KEY` (optional) |
| Playwright jobs | Headless Chromium scraping of `/careers`, `/jobs`, etc. | 0.8 (roles found) | none |
| Layoffs.fyi | Public Google Sheets CSV export, 120-day window | 0.95 (recent match) | none |
| PDL leadership | People Data Labs `/v5/person/search`, 90-day window | 0.85 | `PDL_API_KEY` (optional) |

**LLM fallback (`_llm_enrich`):** After real signals are collected, an LLM call fills any remaining gaps using real signal context as grounding. Real-signal data overrides LLM values when confidence is high enough (≥ 0.5 for Crunchbase, ≥ 0.4 for jobs, ≥ 0.5 for layoffs).

**`CompanyProfile` dataclass** — the central data object passed through the entire pipeline:
```
email, domain, company_name, headcount, funding_stage
recently_funded, had_layoffs, open_engineering_roles
ai_maturity_score (0–3), segment (0–4), enriched_at
crunchbase_signal, job_posts_signal, layoffs_signal, leadership_change_signal
leadership_change (LeadershipChange dataclass)
signals_research (from signals_research.py)
raw (dict with ai_maturity_reason, ai_maturity_signals)
```

**Segment classification (`_classify_segment`)** — priority-ordered:
```
Priority 1: leadership_change.detected          → segment 3 (leadership_transition)
Priority 2: recently_funded AND ≥3 eng roles    → segment 1 (recently_funded)
Priority 3: had_layoffs (120-day window)        → segment 2 (restructuring_cost)
Priority 4: ai_maturity_score ≥ 2              → segment 4 (capability_gap)
Default:                                        → segment 0 (generic)
```

---

### `agent/ai_maturity.py` — AI Maturity Scorer
**Responsibility:** Deterministic scoring (0–3) of a company's AI/ML investment level. Replaced pure LLM delegation to prevent hallucinated scores.

**8-priority ladder:**
1. `_HIGH_AI_TITLES` regex on Playwright job titles (e.g. "LLM engineer", "MLOps")
2. GitHub repo names / commit counts (optional `github_signals` dict)
3. AI role ratio: `ai_role_count / open_engineering_roles`
4. Patent titles (optional `patent_signals` dict)
5. Domain root heuristic (e.g. `ai`, `ml`, `neural` in domain)
6. Conference talk titles (optional `conference_signals` dict)
7. Funding-stage proxy (Series D+ correlates with AI investment)
8. LLM estimate — floor only, capped at 2 (cannot score 3 without real signals)

**Output:** `{"score": int, "reason": str, "signals": {...}}` — the `reason` and `signals` are stored in `CompanyProfile.raw` for Langfuse tracing.

---

### `agent/signals_research.py` — Pre-Email Web Research
**Responsibility:** Playwright scrape of the prospect's public website to extract personalization hooks for the email composition step.

**Extracts (in order of priority):**
1. `tagline` — meta description → og:description → first `<h1>`
2. `recent_post` — first article headline on blog/news page (followed via nav link)
3. `product_hint` — page title from `/pricing`, `/product`, `/features`, or `/platform`
4. `tech_hints` — script-src domains matched against known tech stack patterns (Stripe, Datadog, AWS, etc.)

**`CompanySignals.personalization_hook()`** returns a one-liner: `"Saw your recent post: ..."` if a post exists, otherwise the tagline. This is injected into the email composition prompt.

Never raises — errors land in `CompanySignals.error` field.

---

### `agent/email_outreach.py` — Email Composition and Delivery
**Responsibility:** LLM-compose segment-aware emails, validate tone with two gates, send via Resend API, handle delivery events.

**Two-gate composition flow:**
```
compose()
   ↓
Gate 1: _deterministic_tone_check()    ← fast regex (banned words, word count, AI mention, FM-1 commitment language)
   ↓ if fail: retry compose()
Gate 2: tone_check()                   ← LLM "YES/NO" style check
   ↓ proceed regardless (max 2 compose calls)
send()
```

**`_SEGMENT_HINTS`** — per-segment prompt injections:
- Segment 1 (recently_funded): hiring velocity angle
- Segment 2 (restructuring_cost): cost efficiency angle
- Segment 3 (leadership_transition): clean partnership, no lock-in
- Segment 4 (capability_gap): ML platform / agentic build, project-based consulting
- Segment 0 (generic): warm diagnostic question

**Signal-confidence-aware phrasing:** email prompt adjusts language certainty based on average confidence of job_posts + crunchbase signals (HIGH ≥ 0.7 → assert; MEDIUM ≥ 0.4 → hedge; LOW → ask).

**Kill-switch guard:**
- `_OUTBOUND_LIVE` env var (default `false`) — routes all sends to `_STAFF_SINK` email when off
- Startup `RuntimeError` if `OUTBOUND_LIVE=true` but `OUTBOUND_LIVE_APPROVED_BY` is not set (prevents accidental live sends)

**`SEGMENT_LABELS` dict** — canonical mapping used across `main.py`, `competitor_gap.py`, and `hubspot_sync.py`:
```python
{0: "generic", 1: "recently_funded", 2: "restructuring_cost",
 3: "leadership_transition", 4: "capability_gap"}
```

---

### `agent/conversation_handler.py` — Reply Handling and Qualification
**Responsibility:** Maintain per-lead conversation history, generate LLM replies, qualify after N turns.

**State transitions within this module:**
```
any status → "in_conversation"   (on reply received)
             → "qualified"        (if _qualify() returns YES after QUALIFY_AFTER_TURNS=3)
             → "disqualified"     (if _qualify() returns NO after QUALIFY_AFTER_TURNS=3)
```

**History management:** `MAX_HISTORY = 40` entries (~20 exchange turns). Trimmed after every append with `lead.history = lead.history[-MAX_HISTORY:]`.

**System prompt (`_SYSTEM_PROMPT`):** Instructs the agent to be warm, keep replies under 80 words, address offshore objections directly (timezone overlap, English proficiency, seniority), and guide toward a 30-minute discovery call.

**`_conv_post()`** is `@http_retry`-decorated. Both `_llm_reply()` and `_qualify()` use it.

---

### `agent/booking_handler.py` — Cal.com Booking
**Responsibility:** Find the next available business-hours slot and POST a booking to Cal.com.

**Flow:**
1. `_get_event_type_id()` — finds the event type matching `CALCOM_EVENT_TYPE_SLUG` ("discovery-call")
2. `_next_available_slot()` — fetches `/api/v1/slots` for the next 7 days, filters by `_is_business_hours()` (configurable via `BIZ_HOUR_START`/`BIZ_HOUR_END`, defaults 09:00–17:00 UTC) — FM-2 compliance
3. `_calcom_post()` — creates the booking with Google Meet location

Returns `ToolResult(ok, data={booking_url, slot})`. Fails gracefully — errors do not raise; they return `ToolResult(ok=False, error=...)`.

---

### `agent/hubspot_sync.py` — CRM Sync
**Responsibility:** Upsert contacts in HubSpot, mark bounces/complaints, log email activity.

**Key behaviors:**
- `_headers()` function (not module-level dict) — calls `os.getenv("HUBSPOT_API_KEY")` on each request, supports token rotation
- `upsert_contact()` — searches by email, creates if not found, updates if found; handles 409 conflict by extracting existing contact ID from error message
- `mark_bounced()` — sets `hs_email_optout=True` and `lifecycle_stage=disqualified` on hard/complaint bounces
- All four httpx helpers (`_hs_search`, `_hs_create`, `_hs_patch`, `_hs_engage`) are `@http_retry`-decorated
- Errors in `mark_bounced()` and `log_email_activity()` are logged via `log_span` instead of silently swallowed

---

### `agent/sms_handler.py` — Africa's Talking SMS Channel
**Responsibility:** Send outbound SMS and route inbound SMS through the warm-lead channel gate.

**Two-layer warm-lead gate:**
```
Layer 1 (main.py /webhooks/sms):
  db.get_by_phone(phone) → None means cold lead → reject immediately

Layer 2 (handle_inbound_sms):
  lead_status not in {"outreach_sent", "in_conversation", "qualified"} → reject
  (status "new" = no prior email contact → email first)
```

SMS is **never** used for cold outreach. `send_sms()` and `send_booking_confirmation_sms()` are called only from `_run_reply_pipeline` in `main.py`, after booking confirmation.

`parse_at_payload()` normalises Africa's Talking's `application/x-www-form-urlencoded` field names to internal names (`from` → `phone`, etc.).

---

### `agent/competitor_gap.py` — Competitor Intelligence
**Responsibility:** Generate a structured competitor gap brief for the prospect, identifying the 3 most likely competitors Tenacious faces and the strongest outreach angles.

**Output schema:** `prospect, domain, industry, segment, ai_maturity_score, competitors[], recommended_angle, top_gap_summary, generated_at`

**`_infer_industry()`** — regex patterns on job titles and domain to classify into: AI/ML, Fintech, Healthtech, Commerce/Logistics, DevTools/Infrastructure, Cybersecurity, B2B SaaS, EdTech, Media/AdTech, or B2B Tech (default).

**Fallback competitors** — if LLM is unavailable or returns invalid JSON, three static fallbacks are used: Toptal, Andela, Upwork Enterprise — each with pre-written gap analysis.

**`_gap_post()`** is `@http_retry`-decorated. LLM called with `temperature=0.3` and `response_format: json_object`.

---

### `agent/db.py` — Lead Persistence
**Responsibility:** SQLite-backed lead store using SQLAlchemy 2.0.

**`Lead` dataclass** (value object, not ORM):
```
email, lead_id, phone, status (LeadStatus), history (list[dict])
turns, created_at, profile (dict), booking_url, hubspot_contact_id
```

**`LeadStatus`** literal type: `"new" | "outreach_sent" | "in_conversation" | "qualified" | "disqualified"`

**`StaticPool`** — all FastAPI worker threads share one SQLite connection without `check_same_thread` errors. The `_session()` context manager handles commit/rollback/close.

**`ping()`** — executes `SELECT 1` to verify DB connectivity; called by `/health`.

---

### `agent/langfuse_logger.py` — Observability
**Responsibility:** Ship traces and spans to Langfuse cloud; fall back to local JSONL if Langfuse is unreachable.

**`log_trace(name, input, output, metadata, session_id)`** — creates a root trace, returns `trace_id` used by all downstream `log_span()` calls.

**`log_span(trace_id, name, input, output, metadata, level)`** — creates a generation span nested under the trace.

**`_langfuse_post()`** is `@http_retry(attempts=2, base=0.5, cap=3.0)`-decorated. When retries are exhausted, the outer `except Exception` writes to `langfuse_fallback.jsonl` (path configurable via `LANGFUSE_FALLBACK_LOG`).

---

### `agent/retry.py` — HTTP Retry Decorator
**Responsibility:** Exponential backoff with jitter for transient network failures. No external dependencies.

```
delay = min(base * 2^attempt, cap) + uniform(0, 1)
```

**Retried errors:** `httpx.TimeoutException`, `httpx.ConnectError`, `httpx.RemoteProtocolError`, `httpx.ReadError`

**Not retried:** `RuntimeError`, `ValueError`, 4xx/5xx responses (callers must check `status_code`)

**Usage patterns:**
```python
@http_retry                          # defaults: attempts=3, base=1.0, cap=8.0
@http_retry(attempts=2, base=0.5)   # custom settings
```

---

### `agent/tool_result.py` — Result Envelope
**Responsibility:** Consistent return type for all agent tool calls.

```python
@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str = ""
```

`unwrap()` raises `RuntimeError(error)` if `ok=False`. `to_dict()` flattens `data` dict keys into the result dict.

---

## 3. Full Request Pipelines

### 3a. First Contact (new lead email)

```
POST /webhooks/email
  │
  ├── HMAC signature verify (skip if no RESEND_WEBHOOK_SECRET)
  ├── lead.status == "new" OR no thread_id
  └── background_tasks.add_task(_run_bg, _run_full_pipeline, email, text)
           │
           ▼  (background thread)
    lf.log_trace("first_contact", ...)          → trace_id
    enrich_mod.enrich(email)                    → CompanyProfile (4 sources parallel)
    signals_mod.research(profile.domain)        → CompanySignals (personalization)
    profile.signals_research = signals.to_dict()
    gap_mod.generate_competitor_gap_brief(...)  → competitor brief dict
    email_mod.compose_and_send(profile, ...)    → Resend API (or staff sink)
    lead = db.get_or_create(email)
    lead.status = "outreach_sent"
    lead.profile = {enrichment + competitor brief}
    hs.upsert_contact(...)                      → HubSpot contact ID
    lead.hubspot_contact_id = contact_id
    db.save_lead(lead)
    lf.log_trace("first_contact_complete", ...)
```

### 3b. Reply Pipeline (returning lead, email or SMS)

```
POST /webhooks/email  (with thread_id, existing lead)
  OR
POST /webhooks/sms    (warm lead, phone resolved to email)
  │
  └── background_tasks.add_task(_run_bg, _run_reply_pipeline, identifier, text)
           │
           ▼  (background thread)
    lf.log_trace("reply", ...)
    conv.handle_reply(identifier, text, trace_id)
      ├── load lead from db
      ├── append user message to history
      ├── _llm_reply(history)             → agent reply text
      ├── append agent reply to history
      ├── trim history to MAX_HISTORY=40
      ├── if turns >= 3: _qualify(lead)   → YES/NO
      ├── lead.status = "qualified" | "disqualified"
      └── db.save_lead(lead)
    if qualified AND no booking_url:
      booking.book(email, name, trace_id)   → ToolResult(ok, {booking_url, slot})
      hs.upsert_contact(... booking_url)    → HubSpot update
      if lead.phone AND booking ok:
        sms.send_booking_confirmation_sms() → Africa's Talking SMS
    db.save_lead(lead)
    lf.log_trace("reply_complete", ...)
```

### 3c. Bounce / Complaint Events

```
POST /webhooks/email  (type = "email.bounced" | "email.complained")
  │
  ├── email_mod.handle_bounce(email, type, reason) → structured dict
  ├── hs.mark_bounced(email, type)                 → HubSpot optout
  ├── lead.status = "disqualified"
  └── db.save_lead(lead)
  (synchronous — no background task; returns JSONResponse immediately)
```

---

## 4. Lead State Machine

```
                ┌──────┐
           ─────►  new  │
                └──┬───┘
                   │ _run_full_pipeline
                   ▼
           ┌──────────────┐
           │outreach_sent │
           └──────┬───────┘
                  │ reply received
                  ▼
         ┌─────────────────┐
         │ in_conversation │◄── more replies
         └──────┬──────────┘
                │ turns ≥ 3 → _qualify()
                ├──── YES ──────────────► ┌───────────┐
                │                         │ qualified │ → booking + HubSpot update
                └──── NO ───────────────► └───────────┘
                          │               ┌──────────────┐
                          └──────────────►│ disqualified │
                                          └──────────────┘
               (also from bounce/complaint events)
```

---

## 5. External Integrations

| Service | Purpose | Auth | Fallback |
|---------|---------|------|---------|
| OpenRouter | LLM calls (all modules) | `OPENROUTER_API_KEY` | `MOCK_LLM` flag |
| Resend | Email send + webhook | `RESEND_API_KEY` + `RESEND_WEBHOOK_SECRET` | staff sink when `_OUTBOUND_LIVE=false` |
| Cal.com | Discovery call booking | `CALCOM_API_KEY` | `ToolResult(ok=False)` |
| HubSpot | CRM sync | `HUBSPOT_API_KEY` | logs error span, continues |
| Africa's Talking | Bidirectional SMS | `AFRICA_TALKING_API_KEY` | sandbox mode when username=`sandbox` |
| Crunchbase | Funding / headcount | `CRUNCHBASE_API_KEY` | local CSV (`data/crunchbase/crunchbase-companies-information.csv`) |
| People Data Labs | Leadership change | `PDL_API_KEY` | returns `confidence=0.0` gracefully |
| Layoffs.fyi | Layoff events | none (public CSV) | returns `confidence=0.0` on HTTP error |
| Langfuse | Observability | `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` | `langfuse_fallback.jsonl` |

---

## 6. Configuration Surface (Environment Variables)

| Variable | Default | Effect |
|----------|---------|--------|
| `OPENROUTER_API_KEY` | `""` | Required for LLM calls in all modules |
| `DEV_MODEL` | `openai/gpt-4o-mini` | LLM model used everywhere |
| `TEMPERATURE` | `0.7` | LLM temperature (email compose, conversation) |
| `RESEND_API_KEY` | `""` | Resend email delivery |
| `RESEND_FROM_EMAIL` | `Tenacious Outreach <onboarding@resend.dev>` | Sender identity |
| `RESEND_REPLY_TO` | `""` | Reply-to address |
| `RESEND_WEBHOOK_SECRET` | `""` | Svix HMAC verification; skipped if unset |
| `OUTBOUND_LIVE` | `false` | `true` routes to real recipients (requires `OUTBOUND_LIVE_APPROVED_BY`) |
| `OUTBOUND_LIVE_APPROVED_BY` | `""` | Audit trail; required when `OUTBOUND_LIVE=true` |
| `STAFF_SINK_EMAIL` | `sink@tenacious-pilot.dev` | Sink destination when not live |
| `SIMULATE_TOKEN` | `""` | Required to use `/simulate` endpoints |
| `CALCOM_API_KEY` | `""` | Cal.com booking |
| `CALCOM_API_URL` | `http://localhost:3000` | Cal.com base URL |
| `CALCOM_EVENT_TYPE_SLUG` | `discovery-call` | Event type to book |
| `BIZ_HOUR_START` | `9` | Business hours start (UTC hour) for slot filtering |
| `BIZ_HOUR_END` | `17` | Business hours end (UTC hour) |
| `HUBSPOT_API_KEY` | `""` | HubSpot CRM sync |
| `CRUNCHBASE_API_KEY` | `""` | Crunchbase ODM API; falls back to local CSV if unset |
| `PDL_API_KEY` | `""` | People Data Labs leadership detection; skipped if unset |
| `AFRICA_TALKING_USERNAME` | `sandbox` | AT username; controls sandbox vs live base URL |
| `AFRICA_TALKING_API_KEY` | `""` | AT outbound SMS |
| `AFRICA_TALKING_SENDER_ID` | `Sandbox` | AT sender ID shown to recipient |
| `PLAYWRIGHT_TIMEOUT_MS` | `8000` | Timeout for each Playwright page.goto() call |
| `LANGFUSE_PUBLIC_KEY` | `""` | Langfuse observability |
| `LANGFUSE_SECRET_KEY` | `""` | Langfuse observability |
| `LANGFUSE_BASE_URL` | `https://cloud.langfuse.com` | Langfuse endpoint |
| `LANGFUSE_FALLBACK_LOG` | `langfuse_fallback.jsonl` | Local fallback when Langfuse unreachable |
| `DATABASE_URL` | `sqlite:///./leads.db` | SQLAlchemy DB URL |

---

## 7. Known Constraints and Design Trade-offs

**Playwright is synchronous and blocking.** Both `enrichment_pipeline._scrape_job_posts()` and `signals_research.research()` run headless Chromium synchronously. They are run in a `ThreadPoolExecutor` during enrichment, and the full pipeline is called with `asyncio.to_thread()` from `/simulate`. Under high concurrency, Playwright threads may become the bottleneck.

**SQLite with StaticPool.** Thread-safe for the current single-worker deployment. If you scale to multiple Gunicorn workers, each process gets its own SQLite file and leads will not be shared. Migrate `DATABASE_URL` to PostgreSQL for multi-worker deployments.

**LLM retries in conversation.** `_conv_post()` retries on network errors but not on LLM content errors (empty response, malformed JSON). A failed LLM call raises `RuntimeError`, which `_run_bg()` will catch and log — the lead state will not advance for that turn.

**Crunchbase CSV staleness.** The local CSV at `data/crunchbase/crunchbase-companies-information.csv` is a static snapshot. Funding and headcount data will drift. The API path (`CRUNCHBASE_API_KEY`) avoids this but costs money per call; results are `lru_cache`-d in-process.

**Qualification threshold is fixed at 3 turns.** `QUALIFY_AFTER_TURNS = 3` is a module-level constant, not an env var. Change it in `conversation_handler.py` if you need to tune it.

**Segment 0 is a real segment, not an error.** A company with no strong signals lands in segment 0 (generic). The email composition prompt for segment 0 asks a single warm diagnostic question — this is intentional, not a fallback defect.

---

## 8. Production Readiness Status

Based on the Production Readiness Roadmap, the following items have been addressed and what remains open:

### Resolved (implemented in codebase)

| Item | Fix applied |
|------|------------|
| In-memory lead state (FM-4) | Replaced `_LEADS` dict with SQLite + SQLAlchemy 2.0 (`agent/db.py`); StaticPool for thread safety |
| Webhook rate limiting | `slowapi` applied on all endpoints: 60/min (webhooks), 10/min (`/simulate`) |
| Resend webhook signature | HMAC-SHA256 via Svix headers + 5-minute staleness check in `_verify_resend_signature()` |
| FM-1: over-commits bookings | `_COMMITMENT_WORDS` regex gate in `_deterministic_tone_check()` blocks commitment language before consent |
| FM-2: midnight booking slots | `_is_business_hours()` in `booking_handler.py` filters slots to `BIZ_HOUR_START`–`BIZ_HOUR_END` UTC |
| FM-3: no offshore objection handling | `_SYSTEM_PROMPT` in `conversation_handler.py` includes explicit offshore objection handling instructions |
| `/simulate` unauthenticated | `_check_simulate_auth()` requires `X-Simulate-Token` header; 403 if `SIMULATE_TOKEN` unset |
| Silent background task failures | `_run_bg()` wrapper catches and logs all unhandled exceptions |
| No DB health check | `db.ping()` (SELECT 1) called by `/health`; returns 503 when unreachable |
| No retries on external HTTP calls | `@http_retry` applied to all httpx helpers across every module |
| Langfuse no retries | `_langfuse_post()` decorated with `@http_retry(attempts=2, base=0.5, cap=3.0)` |
| `OUTBOUND_LIVE` no approval gate | Startup `RuntimeError` if `OUTBOUND_LIVE=true` without `OUTBOUND_LIVE_APPROVED_BY` |
| Conversation state machine bug | `lead.status = "qualified" if qualified else "disqualified"` (was `"in_conversation"`) |
| Unbounded conversation history | `MAX_HISTORY = 40` with trim after every append |
| HubSpot silent error suppression | Errors in `mark_bounced` / `log_email_activity` now logged via `log_span` |
| Segment label mismatch | `competitor_gap.py` now imports `SEGMENT_LABELS` from `email_outreach.py` (single source of truth) |
| Blocking event loop in `/simulate` | `await asyncio.to_thread(_run_full_pipeline, ...)` |

### Open / Remaining

| Item | Priority | Notes |
|------|----------|-------|
| Rotate all secrets in git history | **CRITICAL** | `.env` was committed; rotate `RESEND_API_KEY`, `OPENROUTER_API_KEY`, `HUBSPOT_ACCESS_TOKEN`, `CALCOM_API_KEY`, `LANGFUSE_*`, `AFRICA_TALKING_*`, `GMAIL_*` immediately. Add `.env` to `.gitignore`. |
| Core test suite | HIGH | Zero tests on ~2,700 LOC. Minimum: `test_ai_maturity.py` (pure function), `test_email_outreach.py` (tone gates), `test_conversation_handler.py` (state machine), `test_enrichment_pipeline.py` (segment logic) |
| GitHub Actions CI | HIGH | Run `pytest` on every push to catch regressions before deploy |
| Dockerfile | MEDIUM | Playwright/Chromium needs careful layering; without it Render may cold-start and kill Playwright mid-scrape |
| Render plan upgrade | MEDIUM | Free tier sleeps after 15 min inactivity; cold starts kill 8–20s Playwright scrapes. Upgrade to Starter ($7/mo) or add a health-check pinger |
| Alembic migrations | MEDIUM | Currently `metadata.create_all()` on startup; no migration history. Add Alembic before schema changes in production |
| PostgreSQL for multi-worker | LOW (now) | SQLite + StaticPool is safe for one worker; becomes a problem at ≥2 Gunicorn workers |
| `QUALIFY_AFTER_TURNS` as env var | LOW | Currently hardcoded at 3; make configurable without a code deploy |
