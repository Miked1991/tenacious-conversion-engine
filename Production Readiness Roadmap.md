# Production Readiness Roadmap

## 1. CRITICAL — Rotate Secrets Now (Today)

Your `.env` file with real API keys is in git history. This is the most urgent issue:

```bash
# Rotate all of these immediately:
RESEND_API_KEY, OPENROUTER_API_KEY, HUBSPOT_ACCESS_TOKEN,
CALCOM_API_KEY, LANGFUSE_*, AFRICA_TALKING_*, GMAIL_*
```

Then add `.env` to `.gitignore` and use your host's secret manager (Render Environment Variables, not a committed file).

---

## 2. HIGH — Fix In-Memory State (FM-4, ~12h)

`_LEADS` dict in `agent/main.py` is:
- Lost on every restart/crash
- Not thread-safe under concurrent requests
- A GDPR liability

**Fix:** Replace with a lightweight persistent store. For your stack, **Redis** (Render free tier) or **SQLite + SQLAlchemy** is enough. One table: `leads(email TEXT PK, state JSON, updated_at TIMESTAMP)`.

---

## 3. HIGH — Fix the Three Booking Failures (FM-1, FM-2, FM-3, ~62h total)

Documented in `docs/handoff.md`:

| Failure | Fix |
|---|---|
| FM-1: Over-commits bookings in hypergrowth emails | Guard against booking before explicit reply |
| FM-2: Books midnight/2am slots | Filter Cal.com slots to business hours |
| FM-3: No offshore-perception preemption | Add objection-handling turn to conversation flow |

---

## 4. HIGH — Add Rate Limiting + Input Validation

The webhook endpoints (`/webhooks/email`, `/webhooks/sms`) are unauthenticated and unvalidated — any POST will trigger LLM calls and email sends.

```python
# Minimum: verify webhook signatures
# Resend signs payloads; Africa's Talking has username/password
# Add slowapi or fastapi-limiter for rate limiting
```

---

## 5. MEDIUM — Add a Database + Proper Logging

- Add `structlog` or Python's `logging` with JSON output — Langfuse traces are observability, not error logs
- Add a DB migration tool (Alembic) alongside whatever store you pick for FM-4

---

## 6. MEDIUM — Tests + CI (before any external traffic)

Zero tests on 2,685 LOC is the biggest long-term risk. Minimum viable test suite:

```
tests/
  test_enrichment_pipeline.py   # mock 4 external APIs, check segment logic
  test_email_outreach.py        # tone validation, template rendering
  test_ai_maturity.py           # deterministic scorer (pure function, easy)
  test_conversation_handler.py  # state machine transitions
```

Add a GitHub Actions workflow that runs `pytest` on every push.

---

## 7. LOW — Containerize + Upgrade Render Plan

- Add a `Dockerfile` — Playwright/Chromium needs careful layering but is documented
- Render free tier sleeps after 15min inactivity (cold starts kill Playwright) — upgrade to Starter ($7/mo) or add a health-check pinger

---

## Priority Order

| # | Task | Effort | Blocks |
|---|------|--------|--------|
| 1 | Rotate all secrets | 1h | Security breach |
| 2 | Persistent lead state (Redis/SQLite) | 12h | GDPR, reliability |
| 3 | Webhook signature validation + rate limiting | 4h | Abuse, runaway costs |
| 4 | Fix FM-1, FM-2, FM-3 | 62h | Revenue |
| 5 | Structured logging | 4h | Ops visibility |
| 6 | Core test suite | 16h | Safe deploys |
| 7 | GitHub Actions CI | 4h | Catch regressions |
| 8 | Dockerfile + Render upgrade | 8h | Reliability |

**Start with 1 today, then 2 and 3 this week.** Those three unblock safe operation. The booking fixes (4) protect revenue once traffic is flowing.
