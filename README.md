# Tenacious Conversion Engine

A B2B sales automation agent that enriches company profiles, sends personalised
outreach emails, holds multi-turn conversations, qualifies leads, books discovery
calls, and logs everything — evaluated end-to-end with τ²-bench.

> For a complete inheritor guide (setup, ops runbook, failure modes) see
> [docs/handoff.md](docs/handoff.md).

---

## Architecture

```text
                       ┌──────────────────────────────────────────┐
                       │             Incoming Events               │
                       │  Email reply  (Resend webhook)            │
                       │  SMS reply    (Africa's Talking webhook)  │
                       │  Simulation   (POST /simulate)            │
                       └──────────────┬───────────────────────────┘
                                      │
                                      ▼
                       ┌─────────────────────────────┐
                       │     FastAPI Orchestrator     │
                       │       agent/main.py          │
                       └──────┬──────────────┬────────┘
                              │              │
             ┌────────────────▼──┐      ┌────▼────────────────┐
             │  First contact?   │      │  Reply to thread?   │
             └────────┬──────────┘      └──────────┬──────────┘
                      │                            │
                      ▼                            ▼
        ┌─────────────────────────┐   ┌────────────────────────────┐
        │  enrichment_pipeline.py │   │  conversation_handler.py   │
        │  • Crunchbase signal    │   │  • In-memory lead state    │
        │  • Playwright job posts │   │  • LLM reply generation    │
        │  • Layoffs.fyi CSV      │   │  • Lead qualification      │
        │  • PDL leadership chg   │   └──────────┬─────────────────┘
        │  • LLM fallback         │              │ qualified?
        │  • AI maturity score    │              ▼
        └──────────┬──────────────┘   ┌────────────────────────┐
                   │                  │   booking_handler.py   │
                   ▼                  │   • Cal.com API v2     │
        ┌──────────────────────┐      │   • Find open slot     │
        │   email_outreach.py  │      │   • POST /v2/bookings  │
        │   • Compose email    │      └──────────┬─────────────┘
        │   • Tone guard       │                 │
        │   • Resend API send  │                 │
        └──────────┬───────────┘                 │
                   └──────────────┬──────────────┘
                                  │
                                  ▼
                     ┌────────────────────────┐
                     │     hubspot_sync.py    │
                     │  • Upsert contact      │
                     │  • Log email activity  │
                     └────────────┬───────────┘
                                  │
                                  ▼
                     ┌────────────────────────┐
                     │   langfuse_logger.py   │
                     │  • Trace every step    │
                     │  • Span per LLM call   │
                     └────────────────────────┘

External services
─────────────────
  OpenRouter  ──►  LLM calls (qwen/qwen3-next-80b-a3b-instruct)
  Resend      ──►  Outbound email delivery
  Africa's    ──►  Inbound / outbound SMS
  Talking
  HubSpot     ──►  CRM contact records + engagement log
  Cal.com     ──►  Discovery call booking (cloud API v2)
  Langfuse    ──►  Observability traces
  ngrok       ──►  Webhook tunnel (local dev)

Evaluation layer  (tau2-bench/)
────────────────────────────────
  tau2 run  ──►  150 simulations  ──►  eval/score_log.json
                                  ──►  eval/trace_log.jsonl
                                  ──►  eval/baseline.md
```

---

## Repository layout

```text
conversion-engines/
├── agent/                         # Production agent (FastAPI)
│   ├── main.py                    # Orchestrator & webhook endpoints
│   ├── enrichment_pipeline.py     # 4-source enrichment + segment classify
│   ├── ai_maturity.py             # Deterministic AI maturity scoring (0–3)
│   ├── competitor_gap.py          # Competitor gap briefs
│   ├── email_outreach.py          # Email compose, tone-check, Resend send
│   ├── conversation_handler.py    # Multi-turn state + qualification
│   ├── booking_handler.py         # Cal.com discovery-call booking
│   ├── hubspot_sync.py            # HubSpot CRM upsert + activity log
│   └── langfuse_logger.py         # Langfuse trace/span helpers
├── eval/                          # Evaluation deliverables
│   ├── score_log.json             # pass@1, CI, cost, latency
│   ├── trace_log.jsonl            # Per-simulation conversation traces
│   └── baseline.md                # Narrative baseline report
├── scripts/
│   ├── generate_deliverables.py   # Post-process tau2 results → eval/
│   └── measure_latency.py         # 20-interaction p50/p95 measurement
├── tau2-bench/                    # τ²-bench evaluation framework
├── docs/
│   └── handoff.md                 # Full inheritor guide
├── .env.example                   # Credential template
├── requirements.txt               # Agent dependencies
└── README.md
```

---

## AI Maturity Scoring

`agent/ai_maturity.py` scores each prospect 0–3 deterministically from real signals.
No score is ever purely LLM-derived — the LLM estimate is used only as a floor
(capped at 2) when no real signals fire.

### Score definitions

| Score | Label | Meaning |
|-------|-------|---------|
| 0 | None | No AI signals detected |
| 1 | Emerging | Weak signals — data/analytics roles, funding proxy, or conference mention |
| 2 | Practitioner | Clear AI presence — at least one AI job title, GitHub AI repo, or domain hint |
| 3 | Leader | Strong multi-signal — 3+ AI titles, 30%+ AI role ratio, or 3+ AI repos |

### Signal priority ladder

| Priority | Signal source | Score trigger |
|----------|--------------|---------------|
| 1 | Job-title keywords (LLM / GenAI / MLOps / NLP) | ≥3 high-conf hits → 3; ≥1 → 2 |
| 2 | GitHub AI/ML repos + commit count | ≥3 repos or 200+ AI commits → 3; ≥1 → 2 |
| 3 | AI role ratio (ai_role_count / open_engineering_roles) | ≥30% → 3; ≥10% → 2; >0% → 1 |
| 4 | AI patent filings | ≥3 patents → 3; ≥1 → 2 |
| 5 | Domain-root heuristic (ai/ml/neural/genai…) | → 2 |
| 6 | Conference talk titles (NeurIPS / ICML / ICLR…) | ≥2 talks → 2; ≥1 → 1 |
| 7 | Funding-stage proxy (Series C+ → probable AI spend) | → 1 |
| 8 | LLM estimate (floor only, capped at 2) | → 1 or 2 |

### Output schema

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

To activate GitHub, patent, or conference signals, pass them when calling
`score_ai_maturity()`:

```python
from agent.ai_maturity import score_ai_maturity

result = score_ai_maturity(
    job_signal_value   = {"sample_titles": ["ML Engineer"], "ai_role_count": 2, "open_engineering_roles": 10},
    cb_signal_value    = {"funding_stage": "series_b"},
    domain             = "acme.ai",
    llm_estimate       = 1,
    github_signals     = {"repos": ["llm-serving", "langchain-fork"], "ai_commit_count": 450},
    patent_signals     = {"titles": ["Neural network for text classification — US12345678"]},
    conference_signals = {"talks": ["NeurIPS 2024: Scaling LLM inference"]},
)
# result["score"]   → 3
# result["reason"]  → "GitHub: 2 AI/ML repos, 450 AI commits; AI role ratio=20%..."
```

---

## Competitor Gap Brief

`agent/competitor_gap.py` generates a structured brief each time a new lead is
enriched. The brief is stored in the lead's `profile.competitor_gap_brief` and logged
to Langfuse.

### Output schema

```json
{
  "prospect":          "Acme Corp",
  "domain":            "acme.ai",
  "industry":          "AI/ML",
  "segment":           "recently_funded",
  "ai_maturity_score": 2,
  "competitors": [
    {
      "name": "Toptal",
      "positioning": "Premium global freelance network, claims top 3% of talent",
      "gap_analysis": {
        "hiring_trends":      "2–4 week vetting queue with no East Africa bench",
        "tech_stack_gaps":    ["limited ML/AI specialist depth", "no managed delivery"],
        "funding_comparison": "Late-stage vs Acme's early-growth stage — no urgency to expand",
        "team_growth":        "Relies on freelancer churn; no dedicated engineering bench"
      },
      "tenacious_advantages": [
        "40–60% lower cost at equivalent seniority",
        "2–3 week placement vs Toptal's 4-week vetting",
        "Fully managed — no self-management burden"
      ],
      "outreach_angles": {
        "primary":  "Cut engineering hiring cost by 50% without sacrificing speed or seniority",
        "fallback": "Tenacious places senior engineers 2× faster than Toptal at half the rate"
      },
      "confidence_score": 0.75
    }
  ],
  "recommended_angle": "Cost-effective senior ML engineering with a managed delivery lead",
  "top_gap_summary":   "No competitor offers Acme Corp the combination of East Africa ML specialist talent at 40–60% cost with managed delivery. Tenacious's 2–3 week placement is the fastest path to qualified recently-funded-stage engineers in the AI/ML space.",
  "generated_at":      "2026-04-26T10:00:00+00:00"
}
```

Industry is inferred deterministically from job-post titles and the company domain.
If the LLM is unavailable (no `OPENROUTER_API_KEY`), the full-schema fallback brief
with Toptal / Andela / Upwork Enterprise is returned.

---

## Requirements

### System

| Requirement | Version |
|-------------|---------|
| Python | 3.12, 3.13, or 3.14 |
| Playwright Chromium | latest |
| uv | latest (for τ²-bench install) |
| ngrok | any (webhook tunnel) |

### Python packages (agent)

| Package | Purpose |
|---------|---------|
| `fastapi>=0.111` | Webhook server |
| `uvicorn[standard]>=0.29` | ASGI runner |
| `httpx>=0.27` | Outbound HTTP (all APIs) |
| `python-dotenv>=1.0` | `.env` loading |
| `playwright>=1.44` | Job-post scraping |

### External accounts

| Service | Free tier | Purpose |
|---------|-----------|---------|
| [OpenRouter](https://openrouter.ai) | Yes | LLM gateway |
| [Resend](https://resend.com) | 3,000 emails/mo | Outbound email |
| [Africa's Talking](https://africastalking.com) | Sandbox free | SMS |
| [HubSpot](https://developers.hubspot.com) | Sandbox free | CRM |
| [Cal.com](https://cal.com) | Free | Discovery-call booking |
| [Langfuse](https://cloud.langfuse.com) | 50k traces/mo | Observability |
| [Crunchbase](https://data.crunchbase.com) | Paid | Funding/headcount data |
| [PDL](https://peopledatalabs.com) | Paid trial | Leadership change detection |

---

## Setup

### 1. Clone and install agent dependencies

```bash
git clone <repo-url> conversion-engines
cd conversion-engines
pip install -r requirements.txt
playwright install chromium
```

### 2. Install τ²-bench

```bash
cd tau2-bench
uv sync
cd ..
```

### 3. Configure credentials

```bash
cp .env.example .env   # then edit .env with your keys
```

Required variables:

```env
# LLM
OPENROUTER_API_KEY=sk-or-v1-...
DEV_MODEL=qwen/qwen3-next-80b-a3b-instruct
EVAL_MODEL=anthropic/claude-sonnet-4-6

# Email
RESEND_API_KEY=re_...
RESEND_FROM_EMAIL=Tenacious Outreach <onboarding@resend.dev>

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
CRUNCHBASE_API_KEY=...
PDL_API_KEY=...

# Observability
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com

# Dev flags
MOCK_LLM=false
WEBHOOK_BASE_URL=https://<your-ngrok-url>
```

> **τ²-bench** also needs `tau2-bench/.env`:
>
> ```env
> OPENAI_API_KEY=<same-as-OPENROUTER_API_KEY>
> OPENAI_API_BASE=https://openrouter.ai/api/v1
> ```

### 4. Start ngrok

```bash
ngrok http 8000
# copy the HTTPS forwarding URL into WEBHOOK_BASE_URL in .env
```

### 5. Configure webhooks

| Service | Webhook URL | Event |
|---------|-------------|-------|
| Resend | `https://<ngrok>/webhooks/email` | `email.received` |
| Africa's Talking | `https://<ngrok>/webhooks/sms` | SMS callback |

### 6. Run the agent server

```bash
uvicorn agent.main:app --reload --port 8000
```

Health check: `curl http://localhost:8000/health`

### 7. Simulate a lead end-to-end

```bash
curl -X POST http://localhost:8000/simulate \
  -H "Content-Type: application/json" \
  -d '{"email": "prospect@example.com", "text": "Tell me more."}'
```

The agent will: enrich → classify segment → score AI maturity → generate gap brief →
compose email → tone-check → send via Resend → log to HubSpot → trace to Langfuse.

Send a follow-up to simulate a reply:

```bash
curl -X POST http://localhost:8000/webhooks/email \
  -H "Content-Type: application/json" \
  -d '{"from": "prospect@example.com", "text": "Sounds interesting, tell me more.", "thread_id": "1"}'
```

After three reply turns the agent qualifies the lead and books a Cal.com slot automatically.

### 8. Measure production latency

```bash
python scripts/measure_latency.py --base-url http://localhost:8000
# outputs latency_report.json with p50 / p95 over 20 interactions
```

---

## Running the τ²-bench evaluation

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
```

Then generate the deliverable files:

```bash
cd ..
python scripts/generate_deliverables.py
# writes eval/score_log.json, eval/trace_log.jsonl, eval/baseline.md
```

### Baseline results

| Metric | Value |
|--------|-------|
| pass@1 | **72.67%** |
| 95% CI | [65.04%, 79.17%] |
| Simulations | 150 (30 tasks × 5 trials) |
| Avg agent cost | $0.0199 / simulation |
| Total eval cost | ~$2.99 |
| p50 latency (tau2) | 105.95 s |
| p95 latency (tau2) | 551.65 s |
| p50 latency (agent) | 29.3 s |
| p95 latency (agent) | 36.3 s |
| Infra errors | 0 |

---

## Known Failure Modes

Four failure modes are documented and unmitigated. Address before scaling to production.

| ID | Name | Risk | Effort |
|----|------|------|--------|
| FM-1 | Bench over-commitment in hypergrowth emails | $730K ACV/yr | ~40h |
| FM-2 | Cal.com midnight/2am slot booking | 18pp booking loss | ~10h |
| FM-3 | Offshore-perception objection (no pre-emption) | 30–40% of discovery calls | ~12h |
| FM-4 | In-memory `_LEADS` dict not thread-safe or persistent | GDPR + Resend reputation | ~12h |

Full mitigation plans are in [docs/handoff.md § 11](docs/handoff.md#11-known-failure-modes).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `qwen/qwen3-next-80b-a3b` 400 error | Add `-instruct` suffix: `qwen/qwen3-next-80b-a3b-instruct` |
| Webhooks not arriving | Confirm ngrok is running; paste current HTTPS URL into service dashboard |
| HubSpot 400 on patch | `hs_lead_status` accepts only: `NEW`, `OPEN`, `IN_PROGRESS`, `CONNECTED`, `OPEN_DEAL`, `UNQUALIFIED`, `ATTEMPTED_TO_CONTACT`, `BAD_TIMING` |
| Cal.com 410 on v1 | API v1 is decommissioned; use `https://api.cal.com/v2` with header `cal-api-version: 2024-06-14` |
| Unicode crash on Windows | Set `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` before the `tau2` command |
| State lost between messages | In-memory only; see FM-4 in [docs/handoff.md](docs/handoff.md) |
| All enrichment signals are defaults | `CRUNCHBASE_API_KEY` or `PDL_API_KEY` missing; set in `.env` |
