# Tenacious Conversion Engine — Inheritor Handoff Guide

This document is the single reference for anyone taking over or extending this system.
It covers architecture, module contracts, setup, operations, and the known failure modes
that must be addressed before scaling to production.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture Diagram](#2-architecture-diagram)
3. [Module Reference](#3-module-reference)
4. [Data Schemas](#4-data-schemas)
5. [Setup from Scratch](#5-setup-from-scratch)
6. [Running Locally](#6-running-locally)
7. [Production Deployment (Render)](#7-production-deployment-render)
8. [API Reference](#8-api-reference)
9. [Extension Guide](#9-extension-guide)
10. [Operations Runbook](#10-operations-runbook)
11. [Known Failure Modes](#11-known-failure-modes)
12. [Decision Log](#12-decision-log)

---

## 1. System Overview

The Tenacious Conversion Engine is a B2B sales automation agent for Tenacious, an
East Africa engineering talent firm. The agent:

- **Enriches** a company profile from up to 4 sources (Crunchbase, Playwright job scraping,
  layoffs.fyi, People Data Labs).
- **Classifies** the prospect into one of four sales segments (generic, recently_funded,
  post_layoff, hypergrowth).
- **Scores** AI maturity (0–3) deterministically from job-post signals, GitHub activity,
  patents, conference talks, domain heuristics, and funding stage.
- **Generates** a competitor gap brief (Toptal / Andela / Upwork vs Tenacious).
- **Composes** a segment-aware cold-outreach email, validates it against a deterministic
  tone guard, and sends via Resend.
- **Handles** multi-turn email and SMS conversations, qualifies leads, and books
  discovery calls via Cal.com.
- **Logs** every step to Langfuse for observability and HubSpot for CRM.

All pipeline state is keyed on the prospect's email address.

---

## 2. Architecture Diagram

```
Incoming Events
  ├── Email reply      → POST /webhooks/email  (Resend inbound webhook)
  ├── SMS reply        → POST /webhooks/sms    (Africa's Talking callback)
  └── Simulation       → POST /simulate        (test injection)
             │
             ▼
    ┌─────────────────────────┐
    │   agent/main.py          │  FastAPI orchestrator
    └────────┬────────────────┘
             │
    ┌────────┴──────────────────────────────┐
    │  New lead (first contact)?            │  Existing lead (reply)?
    │                                       │
    ▼                                       ▼
enrichment_pipeline.enrich()        conversation_handler.handle_reply()
  ├── _fetch_crunchbase()              ├── LLM reply generation
  ├── _scrape_job_posts()              ├── Lead qualification scoring
  ├── _parse_layoffs_fyi()             └── Returns (reply_text, qualified: bool)
  ├── _detect_leadership_change()               │
  ├── _llm_enrich()  [fallback]                 │ qualified=True?
  └── score_ai_maturity()                       ▼
             │                        booking_handler.book()
             ▼                          ├── Cal.com GET /event-types
email_outreach.compose_and_send()       ├── Cal.com GET /slots
  ├── compose()                         └── Cal.com POST /bookings
  ├── _deterministic_tone_check()                │
  ├── _llm_tone_check()  [secondary]             │
  └── send()  → Resend API                       │ booking_url returned
             │                                   │
             └──────────────────┬────────────────┘
                                │
                                ▼
                     hubspot_sync.upsert_contact()
                       ├── Create/update contact
                       ├── Set hs_lead_status
                       └── Log email activity
                                │
                                ▼
                     langfuse_logger.log_trace/log_span()

External services
─────────────────
  OpenRouter      ──►  LLM calls  (qwen/qwen3-next-80b-a3b-instruct)
  Resend          ──►  Email delivery
  Africa's Talking──►  SMS inbound/outbound
  HubSpot         ──►  CRM contacts + engagement log
  Cal.com v2      ──►  Discovery-call booking
  Langfuse        ──►  Observability traces
  Crunchbase      ──►  Funding / headcount data  (requires API key)
  PDL             ──►  Leadership change detection (requires API key)
  ngrok           ──►  Local webhook tunnel
```

---

## 3. Module Reference

### `agent/main.py`
**Role:** FastAPI orchestrator. Receives all inbound events, dispatches to sub-modules,
coordinates HubSpot logging.

| Function | Trigger | Description |
|---|---|---|
| `_run_full_pipeline(email, text)` | First contact | Enrich → gap brief → email → HubSpot |
| `_run_reply_pipeline(identifier, text)` | Email/SMS reply | Conversation → qualify → book → SMS confirm |
| `POST /webhooks/email` | Resend webhook | Routes to full or reply pipeline |
| `POST /webhooks/sms` | Africa's Talking | Warm-lead gate → reply pipeline |
| `POST /simulate` | Manual test | Inject synthetic lead |
| `POST /simulate/sms` | Manual test | Inject synthetic SMS lead |
| `GET /health` | Liveness probe | Returns `{"status": "ok"}` |

**State:** Lead objects live in-memory in `conversation_handler._LEADS`. Not thread-safe.
See FM-4 in section 11.

---

### `agent/enrichment_pipeline.py`
**Role:** 4-source company enrichment + segment classification.

| Function | Source | Confidence |
|---|---|---|
| `_fetch_crunchbase(domain)` | Crunchbase ODM v4 | 0.9 found / 0.2 not found |
| `_scrape_job_posts(domain)` | Playwright headless | 0.8 found / 0.4 not found |
| `_parse_layoffs_fyi(company, domain)` | Google Sheets CSV | 0.95 found / 0.6 clean |
| `_detect_leadership_change(domain)` | PDL API | 0.85 found / 0.0 missing key |
| `_llm_enrich(domain, ctx)` | OpenRouter LLM | fallback only |

**Segment classification** (`_classify_segment`):

| Segment | Label | Criteria |
|---|---|---|
| 1 | `recently_funded` | Recently raised Series A/B AND ≥3 open engineering roles |
| 2 | `post_layoff` | Had layoffs in the last 90 days |
| 3 | `hypergrowth` | ≥40% YoY headcount growth, no recent layoffs |
| 0 | `generic` | None of the above |

**Entry point:** `enrich(email: str) -> CompanyProfile`

---

### `agent/ai_maturity.py`
**Role:** Deterministic AI maturity scoring. Returns `{"score": 0–3, "reason": str, "signals": dict}`.

Priority ladder (highest wins):

| Priority | Signal | Score trigger |
|---|---|---|
| 1 | Job-title keyword hits (LLM/GenAI/MLOps) | ≥3 high → 3; ≥1 → 2 |
| 2 | GitHub AI/ML repos + commit count | ≥3 repos or 200+ commits → 3 |
| 3 | AI role ratio (ai_role_count / open_engineering_roles) | ≥30% → 3; ≥10% → 2 |
| 4 | AI patent filings | ≥3 → 3; ≥1 → 2 |
| 5 | Domain-root heuristic (ai/ml/neural…) | → 2 |
| 6 | Conference talk titles (NeurIPS, ICML…) | ≥2 → 2; ≥1 → 1 |
| 7 | Funding-stage proxy (Series C+ correlates) | → 1 |
| 8 | LLM estimate (floor, capped at 2) | → 1 or 2 |

**Entry point:** `score_ai_maturity(job_signal_value, cb_signal_value, domain, llm_estimate, github_signals=None, patent_signals=None, conference_signals=None) -> dict`

---

### `agent/competitor_gap.py`
**Role:** Generates a structured competitor brief for the prospect.

Output includes `industry` (inferred from job titles / domain), 3 competitors each with
full `gap_analysis`, `tenacious_advantages`, `outreach_angles`, and `confidence_score`.
Falls back to hardcoded Toptal / Andela / Upwork Enterprise brief when LLM is unavailable.

**Entry point:** `generate_competitor_gap_brief(profile, trace_id) -> dict`

---

### `agent/email_outreach.py`
**Role:** Email composition, tone validation, Resend delivery.

Tone guard rejects emails containing: disrupt, leverage, synergy, AI, machine learning,
innovative, revolutionize, game-changer, paradigm, bleeding-edge, world-class,
best-in-class, cutting-edge, transformational, empower, unlock, scalable solution,
robust solution. Also enforces ≤120 words in body.

**Entry point:** `compose_and_send(profile, trace_id, dry_run=False) -> dict`

---

### `agent/conversation_handler.py`
**Role:** In-memory multi-turn state, LLM reply generation, lead qualification.

Each lead has a `Lead` object with: `email`, `lead_id`, `status`, `profile`, `turns` list,
`hubspot_contact_id`, `booking_url`, `phone`.

**Warning:** State is lost on process restart. For production, replace `_LEADS` dict with
Redis or a database (see FM-4).

---

### `agent/booking_handler.py`
**Role:** Cal.com v2 discovery-call booking.

Uses `cal-api-version: 2024-06-14` header. The event type slug must match a real event
type in the Cal.com account (`CALCOM_EVENT_TYPE_SLUG` env var).

---

### `agent/hubspot_sync.py`
**Role:** CRM upsert and activity logging via HubSpot Private App token.

Valid `hs_lead_status` values: `NEW`, `OPEN`, `IN_PROGRESS`, `CONNECTED`,
`OPEN_DEAL`, `UNQUALIFIED`, `ATTEMPTED_TO_CONTACT`, `BAD_TIMING`.

---

### `agent/langfuse_logger.py`
**Role:** Langfuse trace and span helpers. Every pipeline step is wrapped in a span.

---

## 4. Data Schemas

### CompanyProfile (dataclass, `enrichment_pipeline.py`)

```python
@dataclass
class CompanyProfile:
    email:                   str
    domain:                  str
    company_name:            str
    headcount:               int
    funding_stage:           str
    recently_funded:         bool
    had_layoffs:             bool
    headcount_growth_pct:    float
    open_engineering_roles:  int
    ai_maturity_score:       int          # 0–3, deterministic
    segment:                 int          # 0–3 (set after __init__)
    crunchbase_signal:       dict         # SignalResult.to_dict()
    job_posts_signal:        dict
    layoffs_signal:          dict
    leadership_change_signal: dict
    leadership_change:       LeadershipChange
    raw:                     dict         # LLM fill-in + ai_maturity_reason/signals
    enriched_at:             str          # ISO-8601 UTC
```

### SignalResult (dataclass)

```python
@dataclass
class SignalResult:
    value:      Any
    confidence: float   # 0.0–1.0
    source:     str
    fetched_at: str     # ISO-8601 UTC
```

### CompetitorGapBrief (dict, `competitor_gap.py`)

```json
{
  "prospect":          "Acme Corp",
  "domain":            "acme.io",
  "industry":          "AI/ML",
  "segment":           "recently_funded",
  "ai_maturity_score": 2,
  "competitors": [
    {
      "name": "Toptal",
      "positioning": "...",
      "gap_analysis": {
        "hiring_trends":      "...",
        "tech_stack_gaps":    ["...", "..."],
        "funding_comparison": "...",
        "team_growth":        "..."
      },
      "tenacious_advantages": ["...", "..."],
      "outreach_angles": {
        "primary":  "...",
        "fallback": "..."
      },
      "confidence_score": 0.80
    }
  ],
  "recommended_angle": "...",
  "top_gap_summary":   "...",
  "generated_at":      "2026-04-26T10:00:00+00:00"
}
```

### AIMaturiyResult (dict, `ai_maturity.py`)

```json
{
  "score": 2,
  "reason": "AI/LLM roles detected (high=1, tools=0); AI role ratio=15%",
  "signals": {
    "job_title_hits":   ["ml engineer"],
    "tool_stack_hits":  [],
    "github_repos":     [],
    "patents":          [],
    "conference_talks": [],
    "ai_role_ratio":    0.15,
    "domain_match":     false
  }
}
```

---

## 5. Setup from Scratch

### Prerequisites

| Tool | Version | Install |
|---|---|---|
| Python | 3.12+ | python.org |
| pip | latest | bundled |
| ngrok | any | ngrok.com |
| uv | latest | `pip install uv` |
| Playwright | latest | `pip install playwright && playwright install chromium` |

### Step 1 — Clone and install

```bash
git clone <repo-url> conversion-engines
cd conversion-engines
pip install -r requirements.txt
playwright install chromium
```

### Step 2 — Install τ²-bench (evaluation only)

```bash
cd tau2-bench
uv sync
cd ..
```

### Step 3 — Configure credentials

```bash
cp .env.example .env
# edit .env with your keys
```

Required environment variables:

```env
# LLM
OPENROUTER_API_KEY=sk-or-v1-...
DEV_MODEL=qwen/qwen3-next-80b-a3b-instruct
EVAL_MODEL=anthropic/claude-sonnet-4-6

# Email
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=Tenacious Outreach <you@yourdomain.com>

# SMS
AFRICA_TALKING_USERNAME=sandbox
AFRICA_TALKING_API_KEY=atsk_...

# CRM
HUBSPOT_ACCESS_TOKEN=pat-...

# Calendar
CALCOM_API_URL=https://api.cal.com
CALCOM_API_KEY=cal_live_...
CALCOM_EVENT_TYPE_SLUG=discovery-call

# Enrichment
CRUNCHBASE_API_KEY=...        # required for live Crunchbase signal
PDL_API_KEY=...               # required for leadership-change signal

# Observability
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com

# Dev flags
MOCK_LLM=false                # set true to skip LLM calls (CI/offline)
WEBHOOK_BASE_URL=https://<ngrok-url>
```

> **τ²-bench** also needs `tau2-bench/.env`:
> ```env
> OPENAI_API_KEY=<same-as-OPENROUTER_API_KEY>
> OPENAI_API_BASE=https://openrouter.ai/api/v1
> ```

### Step 4 — Verify Cal.com event type slug

```bash
curl -s "https://api.cal.com/v2/event-types" \
  -H "Authorization: Bearer $CALCOM_API_KEY" \
  -H "cal-api-version: 2024-06-14" | jq '.data[].slug'
```

The value must match `CALCOM_EVENT_TYPE_SLUG` in `.env`.

### Step 5 — Configure Resend and Africa's Talking webhooks

| Service | Dashboard | Webhook URL |
|---|---|---|
| Resend | resend.com/webhooks | `https://<ngrok>/webhooks/email` |
| Africa's Talking | africastalking.com/sms | `https://<ngrok>/webhooks/sms` |

---

## 6. Running Locally

```bash
# Terminal 1 — ngrok tunnel
ngrok http 8000
# Copy the HTTPS URL into WEBHOOK_BASE_URL in .env

# Terminal 2 — agent server
uvicorn agent.main:app --reload --port 8000

# Health check
curl http://localhost:8000/health
```

### Simulate a first-contact lead

```bash
curl -X POST http://localhost:8000/simulate \
  -H "Content-Type: application/json" \
  -d '{"email": "cto@example.com", "text": "Hi"}'
```

### Simulate a reply (continues the conversation)

```bash
curl -X POST http://localhost:8000/webhooks/email \
  -H "Content-Type: application/json" \
  -d '{"from": "cto@example.com", "text": "Tell me more about pricing.", "thread_id": "1"}'
```

### Mock LLM mode (no API keys needed)

```bash
MOCK_LLM=true uvicorn agent.main:app --reload --port 8000
```

### Run τ²-bench evaluation

```bash
cd tau2-bench
PYTHONIOENCODING=utf-8 PYTHONUTF8=1 \
  .venv/Scripts/tau2 run \
    --domain retail \
    --agent-llm openrouter/qwen/qwen3-next-80b-a3b-instruct \
    --user-llm  openrouter/openai/gpt-4.1 \
    --num-trials 5 \
    --num-tasks  30 \
    --task-split-name test \
    --auto-resume \
    --max-concurrency 5
cd ..
python scripts/generate_deliverables.py
```

---

## 7. Production Deployment (Render)

The `render.yaml` file configures a single web service on Render Free tier.

```
Service type:  Web Service
Build command: pip install -r requirements.txt && playwright install chromium
Start command: uvicorn agent.main:app --host 0.0.0.0 --port $PORT
Health check:  /health
```

Set all env vars from section 5 as Render environment variables. Do not commit `.env`.

**Caveats on Render Free:**
- Instance sleeps after 15 min of inactivity; first request after sleep is slow (~30s).
- No persistent disk — in-memory lead state is lost on redeploy or sleep.
- For production use, add Redis for conversation state (see FM-4).

---

## 8. API Reference

### `POST /simulate`
Inject a synthetic lead (first contact).

Request:
```json
{ "email": "prospect@company.com", "text": "optional initial message" }
```

Response:
```json
{
  "lead_id": "uuid",
  "status": "outreach_sent",
  "send_result": { "email_id": "re_..." },
  "segment": "recently_funded",
  "ai_maturity_score": 2,
  "competitor_gap_brief": { ... }
}
```

### `POST /webhooks/email`
Handle Resend inbound events. Two shapes:

**Inbound reply:**
```json
{ "from": "prospect@company.com", "text": "reply text", "thread_id": "..." }
```

**Bounce/complaint:**
```json
{ "type": "email.bounced", "data": { "email": "...", "bounce_type": "hard", ... } }
```

### `POST /webhooks/sms`
Handle Africa's Talking inbound SMS.
```json
{ "from": "+2519...", "text": "sms text" }
```

### `GET /health`
Returns `{ "status": "ok" }`.

---

## 9. Extension Guide

### Adding a new enrichment signal

1. Add a `_fetch_<source>(domain)` function in `enrichment_pipeline.py` that returns a
   `SignalResult`.
2. Add it to the parallel fetch block in `enrich()`.
3. Pass the signal value into `score_ai_maturity()` via a new optional parameter if it
   affects AI scoring, or store it directly on `CompanyProfile.raw`.
4. Update `_classify_segment()` if the signal should influence segment assignment.

### Adding GitHub/patent/conference signals to AI scoring

`score_ai_maturity()` already accepts optional `github_signals`, `patent_signals`, and
`conference_signals` parameters. To activate them:

```python
ai_maturity = score_ai_maturity(
    job_signal_value=job_val,
    cb_signal_value=cb_val,
    domain=domain,
    llm_estimate=int(raw.get("ai_maturity_score", 1)),
    github_signals={"repos": ["langchain-fork", "llm-serving"], "ai_commit_count": 320},
    patent_signals={"titles": ["Neural network for text classification"]},
    conference_signals={"talks": ["NeurIPS 2024 poster: Scaling LLM inference"]},
)
```

### Adding a new sales segment

1. Add a condition to `_classify_segment()` in `enrichment_pipeline.py`.
2. Add the segment integer to `email_outreach.SEGMENT_LABELS` and `SEGMENT_HINTS`.
3. Add a mock email template in `_MOCK_TEMPLATES`.
4. Add the label to `competitor_gap._SEGMENT_LABELS`.

### Changing the tone guard rules

Edit `email_outreach._BANNED_WORDS` (regex) and `_MAX_BODY_WORDS`. The deterministic
guard fires before the LLM tone check, so changes here take effect immediately.

### Switching the LLM

Change `DEV_MODEL` in `.env`. Any OpenRouter-compatible model slug works. The current
default (`qwen/qwen3-next-80b-a3b-instruct`) gives a good balance of cost and quality
for JSON-structured outputs.

---

## 10. Operations Runbook

### Daily checks

1. **Langfuse traces** — open `https://cloud.langfuse.com`, check for `error` spans in
   the last 24h. Common errors: LLM timeout, Resend 429, Cal.com 404.
2. **HubSpot contacts** — verify new leads are being created with `hs_lead_status=OPEN`.
3. **Webhook connectivity** — confirm ngrok (dev) or Render URL (prod) is reachable.

### Key metrics to monitor

| Metric | Target | Alert threshold |
|---|---|---|
| Email tone-check pass rate | ≥95% | <85% |
| LLM call success rate | ≥98% | <95% |
| Booking success rate | ≥80% of qualified leads | <60% |
| p95 pipeline latency | <60s (end-to-end) | >120s |

### Common incidents

| Symptom | Likely cause | Fix |
|---|---|---|
| All emails fail tone check | Banned word in LLM prompt output | Check `DEV_MODEL`; fall back to `MOCK_LLM=true` |
| `qwen/qwen3-next-80b-a3b` 400 | Missing `-instruct` suffix | Correct slug: `qwen/qwen3-next-80b-a3b-instruct` |
| Webhooks not arriving | ngrok tunnel restarted | Restart ngrok; update Resend/AT dashboards |
| Cal.com 404 on booking | Event type slug mismatch | Verify slug via `GET /v2/event-types` |
| HubSpot 400 on status update | Invalid `hs_lead_status` value | Use only values listed in module ref |
| Unicode crash on Windows | Missing UTF-8 env | Set `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` |
| Leads lose state after restart | In-memory `_LEADS` dict | Expected in current build; fix per FM-4 |

### Rotating API keys

1. Generate new key in the provider dashboard.
2. Update `.env` (local) and Render environment variables (production).
3. Restart the agent server.
4. Verify with `GET /health` and a `/simulate` test.

---

## 11. Known Failure Modes

These four failure modes are documented, unmitigated, and carry the highest
pre-production risk.

### FM-1: Bench Over-Commitment (HIGH — $730K annual ACV risk)

**What happens:** Hypergrowth emails promise specific headcount from the bench without
querying actual bench availability. If a prospect books a discovery call and requests
e.g. "5 ML engineers this week", Tenacious may not have them available, collapsing the
deal.

**Probes:** P11–P13 in `probes/probe_library.md`.

**Fix (est. ~40h):** Add a `bench_oracle` module that queries a Google Sheet or
internal availability API before email composition. Inject availability count into the
email template. Block booking if availability < requested headcount.

---

### FM-2: Timezone Scheduling Edge Cases (MEDIUM — 18pp booking loss)

**What happens:** Cal.com slot selection does not filter for business hours. Slots at
midnight EU / 2am EAT / 6am PT get booked, leading to no-shows.

**Probes:** P27–P30.

**Fix (est. ~6h dev + 4h eval):** In `booking_handler.book()`, filter candidate slots
to `09:00–18:00` in the prospect's inferred timezone (from their domain country code
or HubSpot `country` property).

---

### FM-3: Offshore-Perception Objection (MEDIUM — 30–40% discovery calls)

**What happens:** US/EU prospects frequently object "we need engineers in our timezone"
in discovery calls. The outreach email has no pre-emptive framing for this objection.

**Fix (est. ~12h):** Add a segment-conditional sentence in the email template that
proactively addresses timezone overlap: "All Tenacious engineers work US business-hours
overlap (EST/PST coverage available)." Validate with a new probe set.

---

### FM-4: Multi-Thread Lead-State Leakage (HIGH — GDPR + Resend reputation)

**What happens:** `conversation_handler._LEADS` is a plain Python dict, not thread-safe.
Under concurrent requests (multiple webhooks firing in parallel), lead state can
cross-contaminate. Hard-bounced leads are not persisted, so a webhook replay re-sends
to a suppressed address.

**Fix (est. ~8h dev + 4h harness):**
1. Replace `_LEADS` dict with a Redis-backed store (`redis-py`, key=email, value=JSON).
2. Add a `suppression_list` set in Redis keyed by a `SUPPRESSED:` prefix.
3. Before every send, check `SUPPRESSED:<email>`. If present, drop the request.
4. Use a per-lead distributed lock (`SET NX EX`) to prevent concurrent state writes.

---

## 12. Decision Log

| Date | Decision | Rationale |
|---|---|---|
| 2026-04 | Use OpenRouter instead of direct Anthropic API | Single key for all models; easy model swap during eval |
| 2026-04 | Deterministic AI maturity scoring (not pure LLM) | LLM estimates were inconsistent; priority-ladder gives traceable, reproducible scores |
| 2026-04 | 4-segment taxonomy (generic/funded/layoff/hypergrowth) | Minimal viable segmentation that covers the four highest-value ICP patterns |
| 2026-04 | `MOCK_LLM=true` fallback | Required for CI and offline demos without burning API credits |
| 2026-04 | In-memory lead state (no Redis) | Acceptable for evaluation; must be replaced before scaling (FM-4) |
| 2026-04 | τ²-bench retail domain as proxy | No Tenacious-specific task suite exists yet; retail provides comparable conversation depth |
| 2026-04 | Africa's Talking for SMS | Free sandbox, East Africa coverage aligns with Tenacious's talent base |
