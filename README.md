# Tenacious Conversion Engine

A B2B sales automation agent that enriches company profiles, sends personalised
outreach emails, holds multi-turn conversations, qualifies leads, books discovery
calls, and logs everything — evaluated end-to-end with τ²-bench.

---

## Architecture

```
                         ┌─────────────────────────────────────────┐
                         │           Incoming Events                │
                         │  Email reply (Resend webhook)            │
                         │  SMS reply  (Africa's Talking webhook)   │
                         │  Simulation (POST /simulate)             │
                         └──────────────┬──────────────────────────┘
                                        │
                                        ▼
                         ┌─────────────────────────────┐
                         │      FastAPI Orchestrator    │
                         │        agent/main.py         │
                         └──────┬──────────────┬────────┘
                                │              │
               ┌────────────────▼──┐      ┌───▼─────────────────┐
               │ First contact?    │      │  Reply to thread?    │
               │ (new lead)        │      │  (existing lead)     │
               └────────┬──────────┘      └──────────┬───────────┘
                        │                            │
                        ▼                            ▼
          ┌─────────────────────────┐   ┌────────────────────────────┐
          │  enrichment_pipeline.py │   │  conversation_handler.py   │
          │  • LLM company lookup   │   │  • In-memory state         │
          │  • Segment classify     │   │  • LLM reply generation    │
          │    0 generic            │   │  • Lead qualification      │
          │    1 recently_funded    │   └──────────┬─────────────────┘
          │    2 post_layoff        │              │ qualified?
          │    3 hypergrowth        │              ▼
          └──────────┬──────────────┘   ┌────────────────────────┐
                     │                  │   booking_handler.py   │
                     ▼                  │   • Cal.com API v2     │
          ┌──────────────────────┐      │   • Find open slot     │
          │   email_outreach.py  │      │   • POST /v2/bookings  │
          │   • Compose email    │      └──────────┬─────────────┘
          │   • LLM tone check   │                 │
          │   • Resend API send  │                 │
          └──────────┬───────────┘                 │
                     │                             │
                     └──────────────┬──────────────┘
                                    │
                                    ▼
                       ┌────────────────────────┐
                       │     hubspot_sync.py     │
                       │  • Upsert contact       │
                       │  • Log email activity   │
                       └────────────┬────────────┘
                                    │
                                    ▼
                       ┌────────────────────────┐
                       │    langfuse_logger.py   │
                       │  • Trace every step     │
                       │  • Span per LLM call    │
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

```
conversion-engines/
├── agent/                      # Production agent (FastAPI)
│   ├── main.py                 # Orchestrator & webhook endpoints
│   ├── enrichment_pipeline.py  # Company enrichment + segment classify
│   ├── email_outreach.py       # Email compose, tone-check, Resend send
│   ├── conversation_handler.py # Multi-turn state + qualification
│   ├── booking_handler.py      # Cal.com discovery-call booking
│   ├── hubspot_sync.py         # HubSpot CRM upsert + activity log
│   └── langfuse_logger.py      # Langfuse trace/span helpers
├── eval/                       # Deliverables
│   ├── score_log.json          # pass@1, CI, cost, latency
│   ├── trace_log.jsonl         # Per-simulation conversation traces
│   └── baseline.md             # Narrative baseline report
├── scripts/
│   ├── generate_deliverables.py  # Post-process tau2 results → eval/
│   └── measure_latency.py        # 20-interaction p50/p95 measurement
├── tau2-bench/                 # τ²-bench evaluation framework (subdir)
├── .env                        # Live credentials (never commit)
├── requirements.txt            # Agent dependencies
└── README.md
```

---

## Requirements

### System

| Requirement | Version |
| --- | --- |
| Python | 3.12, 3.13, or 3.14 |
| uv | latest (for τ²-bench install) |
| ngrok | any (webhook tunnel) |

### Python packages (agent)

| Package | Purpose |
| --- | --- |
| `fastapi>=0.111` | Webhook server |
| `uvicorn[standard]>=0.29` | ASGI runner |
| `httpx>=0.27` | Outbound HTTP (all APIs) |
| `python-dotenv>=1.0` | `.env` loading |

### External accounts

| Service | Free tier | What it's used for |
| --- | --- | --- |
| [OpenRouter](https://openrouter.ai) | Yes | LLM gateway (Qwen3, GPT-4.1) |
| [Resend](https://resend.com) | 3 000 emails/mo | Outbound email |
| [Africa's Talking](https://africastalking.com) | Sandbox free | SMS |
| [HubSpot](https://developers.hubspot.com) | Sandbox free | CRM |
| [Cal.com](https://cal.com) | Free | Discovery-call booking |
| [Langfuse](https://cloud.langfuse.com) | 50 k traces/mo | Observability |

---

## Setup

### 1. Clone and install agent dependencies

```bash
git clone <repo-url> conversion-engines
cd conversion-engines
pip install -r requirements.txt
```

### 2. Install τ²-bench

```bash
cd tau2-bench
uv sync
cd ..
```

### 3. Configure credentials

Copy the template and fill in your keys:

```bash
cp .env.example .env   # or edit .env directly
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

# Observability
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://cloud.langfuse.com

# Webhook tunnel
WEBHOOK_BASE_URL=https://<your-ngrok-url>
```

> **Note:** The τ²-bench `.env` at `tau2-bench/.env` needs two extra lines:
> ```env
> OPENAI_API_KEY=<same-openrouter-key>
> OPENAI_API_BASE=https://openrouter.ai/api/v1
> ```

### 4. Start ngrok

```bash
ngrok http 8000
# copy the HTTPS forwarding URL into WEBHOOK_BASE_URL in .env
```

### 5. Configure webhooks

| Service | Webhook URL | Event |
| --- | --- | --- |
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

The agent will: enrich → classify segment → compose email → tone-check → send via
Resend → log to HubSpot → trace to Langfuse.

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
| --- | --- |
| pass@1 | **72.67 %** |
| 95 % CI | [65.04 %, 79.17 %] |
| Simulations | 150 (30 tasks × 5 trials) |
| Avg agent cost | $0.0199 / simulation |
| Total eval cost | ~$2.99 |
| p50 latency (tau2) | 105.95 s |
| p95 latency (tau2) | 551.65 s |
| p50 latency (agent) | 29.3 s |
| p95 latency (agent) | 36.3 s |
| Infra errors | 0 |

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `qwen/qwen3-next-80b-a3b` 400 error | Add `-instruct` suffix — correct slug is `qwen/qwen3-next-80b-a3b-instruct` |
| Webhooks not arriving | Confirm ngrok is running; paste the current HTTPS URL into the service dashboard |
| HubSpot 400 on patch | `hs_lead_status` only accepts: `NEW`, `OPEN`, `IN_PROGRESS`, `CONNECTED`, `OPEN_DEAL`, `UNQUALIFIED`, `ATTEMPTED_TO_CONTACT`, `CONNECTED`, `BAD_TIMING` |
| Cal.com 410 on v1 | API v1 is decommissioned; use `https://api.cal.com/v2` with header `cal-api-version: 2024-06-14` |
| Unicode crash on Windows | Set `PYTHONIOENCODING=utf-8 PYTHONUTF8=1` before the `tau2` command |
| State lost between messages | Current implementation is in-memory; replace `_LEADS` dict in `conversation_handler.py` with Redis for production |
