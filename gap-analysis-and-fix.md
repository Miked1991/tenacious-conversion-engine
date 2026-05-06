# Gap Analysis & Fix — Agent and Tool-Use Internals

**Project:** Tenacious Conversion Engine  
**Date:** 2026-05-06  
**Scope:** `agent/booking_handler.py`, `agent/email_outreach.py`, `agent/enrichment_pipeline.py`, `agent/langfuse_logger.py`, `agent/main.py`

---

## Summary Table

| Gap | Severity | Files Affected | Status |
|-----|----------|----------------|--------|
| No retry logic on external API calls | Critical | booking_handler, email_outreach, enrichment_pipeline | Missing |
| Silent logging failures | High | langfuse_logger | Missing |
| Inconsistent error contracts | High | All agent modules | Partial |
| No formal tool interface/schema | Medium | All agent modules | Missing |
| Sequential enrichment (no parallelism) | Medium | enrichment_pipeline | Missing |
| No result caching | Medium | enrichment_pipeline | Missing |
| Webhook processing blocks the request | Medium | agent/main.py | Missing |
| No health check for dependencies | Low | agent/main.py | Missing |

---

## Gap 1 — No Retry Logic on External API Calls (Critical)

### What exists

Every external HTTP call is a single attempt wrapped in a bare `try/except`. On any failure — timeout, 5xx, network blip — the exception is caught and either re-raised or returned as an error dict. There is no second attempt.

**booking_handler.py:104–115** — Cal.com booking POST:
```python
try:
    resp = httpx.post(
        f"{_CALCOM_URL}/api/v1/bookings",
        ...
        timeout=20,
    )
    ...
except Exception as exc:
    result = {"success": False, "booking_url": "", "slot": slot, "error": str(exc)}
```

**email_outreach.py:238–246** — Resend send:
```python
try:
    resp = httpx.post(
        "https://api.resend.com/emails",
        ...
        timeout=20,
    )
    result = resp.json()
except Exception as exc:
    result = {"error": str(exc)}
```

**enrichment_pipeline.py:228–253** — Crunchbase ODM:
```python
try:
    resp = httpx.post(
        "https://api.crunchbase.com/api/v4/searches/organizations",
        ...
        timeout=15,
    )
    ...
except Exception as exc:
    return SignalResult(value={"error": str(exc)}, confidence=0.0, ...)
```

Same pattern in `_parse_layoffs_fyi` (line 391), `_detect_leadership_change` (line 475), and `_llm_enrich` (line 556).

### What's missing

- No retry on transient failures (timeout, 503, 429 rate-limit)
- No exponential backoff — a retry fired immediately hits a struggling service again
- No jitter — if multiple leads are processed in parallel, retries fire at the same instant (thundering herd)
- No retry budget — a misconfigured service could loop forever without a max-attempt ceiling

### Impact

A Cal.com timeout at booking time means the prospect is qualified but never booked. A Resend timeout means the outreach email is never sent. A Crunchbase timeout means the profile is enriched entirely by LLM guesses (confidence = 0.0), degrading segment classification.

### Fix

Install `tenacity` (`pip install tenacity`) and wrap each external call with a shared retry decorator. Three attempts, exponential backoff (1s → 2s → 4s), with jitter.

**Shared retry decorator — add to a new `agent/retry.py`:**
```python
from tenacity import retry, stop_after_attempt, wait_exponential, wait_jitter, retry_if_exception_type
import httpx

_TRANSIENT = (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError)

http_retry = retry(
    retry=retry_if_exception_type(_TRANSIENT),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8) + wait_jitter(max=1),
    reraise=True,
)
```

**Apply to booking_handler.py `_next_available_slot` and `book`:**
```python
from agent.retry import http_retry

@http_retry
def _call_calcom_post(url, headers, json, timeout):
    return httpx.post(url, headers=headers, json=json, timeout=timeout)

# Inside book():
resp = _call_calcom_post(
    f"{_CALCOM_URL}/api/v1/bookings",
    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    json=payload,
    timeout=20,
)
```

**Apply to email_outreach.py `send`:**
```python
from agent.retry import http_retry

@http_retry
def _call_resend(payload, key):
    return httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {key}"},
        json=payload,
        timeout=20,
    )
```

**Apply to enrichment_pipeline.py `_fetch_crunchbase`, `_parse_layoffs_fyi`, `_detect_leadership_change`, `_llm_enrich`:**
```python
from agent.retry import http_retry

@http_retry
def _crunchbase_post(key, body):
    return httpx.post(
        "https://api.crunchbase.com/api/v4/searches/organizations",
        params={"user_key": key},
        json=body,
        timeout=15,
    )
```

> **Decision boundary:** Do NOT retry Playwright scraping (`_scrape_job_posts`) — it is stateful (browser session) and slow. A failed scrape returns `confidence=0.1`, which is the correct graceful degradation. Do NOT retry 4xx responses (bad request, auth failure) — these are permanent errors, not transient.

---

## Gap 2 — Silent Logging Failures (High)

### What exists

`langfuse_logger.py:45–53` and `84–91` catch all exceptions silently:

```python
try:
    httpx.post(
        f"{_BASE_URL}/api/public/ingestion",
        json=payload,
        auth=_auth(),
        timeout=10,
    )
except Exception:
    pass   # ← entire trace/span is dropped with no indication
```

If Langfuse is unreachable (DNS failure, expired credentials, service outage), every `log_trace` and `log_span` call across the pipeline returns successfully. The caller has no way to know the trace was never recorded.

### What's missing

- No fallback when Langfuse is down
- No warning to the caller or operator
- No local write path to preserve observability data

### Impact

During a Langfuse outage, the entire conversation pipeline runs invisibly. Failures in enrichment, tone check, and booking are undetectable after the fact. Debugging becomes impossible.

### Fix

Add a file-based fallback that appends dropped events to a local JSONL file. The main try/except logs a warning before falling back — the pipeline is never blocked, but no event is silently lost.

**Updated `langfuse_logger.py`:**
```python
import json
import logging

_FALLBACK_PATH = os.getenv("LANGFUSE_FALLBACK_LOG", "langfuse_fallback.jsonl")
_log = logging.getLogger(__name__)

def _write_fallback(event_type: str, payload: dict) -> None:
    try:
        with open(_FALLBACK_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event_type": event_type, **payload}) + "\n")
    except Exception:
        pass  # truly last resort — if file write fails, we accept the loss

# Inside log_trace and log_span, replace the bare except:
    except Exception as exc:
        _log.warning("langfuse_unavailable name=%s err=%s — writing to fallback", name, exc)
        _write_fallback("trace" if is_trace else "span", payload["batch"][0]["body"])
```

---

## Gap 3 — Inconsistent Error Contracts (High)

### What exists

Agent modules have three different conventions for reporting failure, mixed arbitrarily:

| Convention | Example |
|-----------|---------|
| Raise `RuntimeError` | `conversation_handler.py:66` — LLM call fails → `raise RuntimeError(...)` |
| Return error dict | `sms_handler.py:126` — returns `{"error": str(exc)}` |
| Return `{"success": False, ...}` | `booking_handler.py:115` — returns `{"success": False, ..., "error": str(exc)}` |
| Silent swallow | `langfuse_logger.py:52` — `except Exception: pass` |

`agent/main.py:185` checks `if result["qualified"]` after `conv.handle_reply()` — but if `handle_reply` raises a `RuntimeError`, the webhook endpoint crashes with a 500 and no structured response is returned to Resend, which may retry the webhook.

### What's missing

A single, consistent `ToolResult` contract so callers never need to guess which convention a function uses.

### Fix

Define a lightweight result envelope. No external library needed — a plain dataclass.

**Add to `agent/tool_result.py`:**
```python
from dataclasses import dataclass, field
from typing import Any

@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str = ""

    def unwrap(self):
        """Return data or raise RuntimeError."""
        if not self.ok:
            raise RuntimeError(self.error)
        return self.data
```

**Migrate `booking_handler.book()` to return `ToolResult`:**
```python
from agent.tool_result import ToolResult

def book(email, name, trace_id, api_key="") -> ToolResult:
    ...
    try:
        resp = _call_calcom_post(...)
        data = resp.json()
        booking_url = ...
        return ToolResult(ok=True, data={"booking_url": booking_url, "slot": slot})
    except Exception as exc:
        return ToolResult(ok=False, error=str(exc))
```

**Caller in `agent/main.py:188`:**
```python
booking_result = booking.book(identifier, name, trace_id, api_key=_CALCOM_API_KEY)
if booking_result.ok:
    lead.booking_url = booking_result.data.get("booking_url", "")
else:
    logger.error("booking_failed identifier=%s err=%s", identifier, booking_result.error)
```

> Migrate modules one at a time. Do not mix old and new conventions inside the same call chain.

---

## Gap 4 — No Formal Tool Interface / Schema (Medium)

### What exists

Every tool is a plain Python function imported directly in `agent/main.py:47–56`:

```python
from agent import enrichment_pipeline as enrich_mod
from agent import email_outreach as email_mod
from agent import conversation_handler as conv
...
```

There are no schema definitions, no metadata, no capability declarations. You cannot expose these tools to an external LLM agent (e.g., Claude tool-use loop) without rewriting the entire interface layer.

### What's missing

- Structured tool definitions (`name`, `description`, `input_schema`, `output_schema`)
- A tool registry that can enumerate available tools at runtime
- A unified invocation path that validates inputs before calling the function

### Fix

Define a minimal tool manifest. This does not require an external framework — a simple registry dict is enough.

**Add to `agent/tool_registry.py`:**
```python
from dataclasses import dataclass
from typing import Callable, Any

@dataclass
class Tool:
    name: str
    description: str
    fn: Callable
    input_keys: list[str]   # required input keys
    output_keys: list[str]  # guaranteed output keys on success

_registry: dict[str, Tool] = {}

def register(tool: Tool):
    _registry[tool.name] = tool

def get(name: str) -> Tool | None:
    return _registry.get(name)

def list_tools() -> list[str]:
    return list(_registry.keys())
```

**Register the booking tool:**
```python
from agent import tool_registry, booking_handler

tool_registry.register(tool_registry.Tool(
    name="book_discovery_call",
    description="Book a Cal.com discovery call for a qualified prospect.",
    fn=booking_handler.book,
    input_keys=["email", "name", "trace_id"],
    output_keys=["booking_url", "slot"],
))
```

> This is a foundation. Claude tool-use integration requires converting `Tool` definitions to Anthropic's JSON schema format — that is a separate step once the registry is populated.

---

## Gap 5 — Sequential Enrichment (Medium)

### What exists

`enrichment_pipeline.enrich()` calls all four sources serially (lines 621–624):

```python
cb_signal      = _fetch_crunchbase(domain)      # up to 15s
job_signal     = _scrape_job_posts(domain)       # up to 12s × 5 URLs = 60s worst case
layoffs_sig    = _parse_layoffs_fyi(...)         # up to 15s
leadership_sig = _detect_leadership_change(...)  # up to 15s
```

Total worst-case latency: ~105 seconds for a single `enrich()` call before `compose_and_send` can begin. The FastAPI webhook handler (`_run_full_pipeline`) does this synchronously — a slow enrichment blocks the HTTP response and risks a Resend webhook timeout.

### What's missing

Concurrent execution of the four independent signal sources.

### Fix

Use `concurrent.futures.ThreadPoolExecutor` (no async rewrite required — all four functions are sync and independent).

**Updated `enrich()` in `enrichment_pipeline.py`:**
```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def enrich(email: str) -> CompanyProfile:
    domain = _extract_domain(email)

    tasks = {
        "cb":        lambda: _fetch_crunchbase(domain),
        "jobs":      lambda: _scrape_job_posts(domain),
        "layoffs":   lambda: _parse_layoffs_fyi(domain.split(".")[0].title(), domain),
        "leadership":lambda: _detect_leadership_change(domain),
    }

    results = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = SignalResult(
                    value={"error": str(exc)}, confidence=0.0, source=f"{key}_error"
                )

    cb_signal      = results["cb"]
    job_signal     = results["jobs"]
    layoffs_sig    = results["layoffs"]
    leadership_sig = results["leadership"]
    # ... rest of merge logic unchanged
```

Worst-case latency drops from ~105s to ~60s (Playwright bound). Typical case: ~15s (Crunchbase / layoffs bound).

---

## Gap 6 — No Result Caching for Enrichment (Medium)

### What exists

Every call to `enrich(email)` re-fetches all four sources from scratch. If the same domain is processed twice (e.g., two contacts from the same company), it hits Crunchbase, Layoffs.fyi, and PDL redundantly.

### What's missing

A short-lived in-process cache keyed by domain. TTL of 1 hour is enough — enrichment data does not change minute-to-minute.

### Fix

```python
import functools

@functools.lru_cache(maxsize=256)
def _fetch_crunchbase_cached(domain: str) -> SignalResult:
    return _fetch_crunchbase(domain)

@functools.lru_cache(maxsize=256)
def _parse_layoffs_fyi_cached(company_name: str, domain: str) -> SignalResult:
    return _parse_layoffs_fyi(company_name, domain)
```

`lru_cache` is in-process only and does not survive restarts — acceptable for this use case. `_scrape_job_posts` should NOT be cached because Playwright state is not thread-safe across cached calls.

---

## Gap 7 — Webhook Processing Blocks the Request (Medium)

### What exists

Both `_run_full_pipeline` and `_run_reply_pipeline` are called synchronously inside the FastAPI webhook handlers. The entire enrichment + compose + send sequence must complete before the HTTP response is returned to Resend.

Resend's webhook delivery timeout is 5 seconds. A full pipeline run (enrichment alone can take 15–60s) will always exceed this, causing Resend to mark the delivery as failed and retry — potentially triggering duplicate outreach.

### What's missing

Background task execution so the webhook returns 200 immediately and the pipeline runs out-of-band.

### Fix

FastAPI's built-in `BackgroundTasks` is the zero-dependency fix:

```python
from fastapi import BackgroundTasks

@app.post("/webhooks/email")
async def webhook_email(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    # ... signature verification ...

    data = await request.json()
    email = data.get("from", {}).get("email", "")

    background_tasks.add_task(_run_full_pipeline, email, data.get("text", ""))
    return JSONResponse({"status": "accepted"}, status_code=200)
```

The handler returns `200 accepted` immediately. Resend sees a success. The pipeline runs in the background thread pool managed by FastAPI's default thread executor.

---

## Gap 8 — Health Check Does Not Verify Dependencies (Low)

### What exists

`GET /health` (agent/main.py) returns a static `{"status": "ok"}` with no validation that downstream services (Langfuse, Resend, Cal.com, Crunchbase) are reachable.

### Fix

Add a lightweight dependency probe that checks critical services without expensive calls:

```python
@app.get("/health")
async def health():
    checks = {}

    # Langfuse: check if credentials are configured
    checks["langfuse"] = "configured" if (
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    ) else "missing_credentials"

    # Resend: check if key is set
    checks["resend"] = "configured" if os.getenv("RESEND_API_KEY") else "missing_key"

    # Cal.com: check if URL is reachable (HEAD only, no auth)
    try:
        httpx.head(os.getenv("CALCOM_API_URL", "http://localhost:3000"), timeout=2)
        checks["calcom"] = "reachable"
    except Exception:
        checks["calcom"] = "unreachable"

    ok = all(v != "unreachable" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                        status_code=200 if ok else 503)
```

---

## Recommended Fix Order

1. **Retry logic** — highest leverage, least code. Add `agent/retry.py` and wrap the five HTTP call sites. Fixes the most critical reliability gap immediately.
2. **Logging fallback** — prevents silent data loss during Langfuse outages. One file change (`langfuse_logger.py`).
3. **Webhook background tasks** — prevents duplicate Resend deliveries caused by slow pipeline blocking the response. One-line change per endpoint.
4. **Consistent error contracts** — add `agent/tool_result.py`, migrate `booking_handler` first (highest call-site impact), then `email_outreach.send`.
5. **Concurrent enrichment** — reduces worst-case latency from ~105s to ~60s. Medium complexity; use `ThreadPoolExecutor`.
6. **Tool registry** — foundation for future Claude tool-use integration. Low urgency, medium effort.
7. **Result caching + health check** — low complexity, low urgency.
