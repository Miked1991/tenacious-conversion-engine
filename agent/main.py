"""
FastAPI orchestrator – the single entry point for all incoming webhooks.

Endpoints
---------
POST /webhooks/email   – Resend inbound email + bounce/complaint events
POST /webhooks/sms     – Africa's Talking inbound SMS (warm leads only)
POST /simulate         – synthetic lead injection for end-to-end testing
GET  /health           – liveness check

Security
--------
/webhooks/email verifies Resend/Svix HMAC signatures when RESEND_WEBHOOK_SECRET
is configured.  /webhooks/sms verifies the AT username in the payload.
Both webhook endpoints are rate-limited to 60 req/min per IP via slowapi.
/simulate and /simulate/sms require X-Simulate-Token header matching SIMULATE_TOKEN
and are rate-limited to 10 req/min per IP.

SMS channel hierarchy
---------------------
/webhooks/sms enforces a two-layer warm-lead gate:

  Layer 1 (here): resolve the inbound phone to an email-keyed lead via
      db.get_by_phone().  If no matching warm lead is found, reject immediately.

  Layer 2 (sms_handler.handle_inbound_sms): double-checks lead_status is in
      {"outreach_sent", "in_conversation", "qualified"} before routing to
      _run_reply_pipeline.
"""

import asyncio
import base64
import dataclasses as _dc
import hashlib
import hmac
import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

load_dotenv()

from agent import db
from agent import enrichment_pipeline as enrich_mod
from agent import email_outreach as email_mod
from agent import conversation_handler as conv
from agent import booking_handler as booking
from agent import hubspot_sync as hs
from agent import langfuse_logger as lf
from agent import sms_handler as sms_mod
from agent import competitor_gap as gap_mod
from agent import signals_research as signals_mod

_CALCOM_API_KEY        = os.getenv("CALCOM_API_KEY", "")
_AT_USERNAME           = os.getenv("AFRICA_TALKING_USERNAME", "sandbox")
_RESEND_WEBHOOK_SECRET = os.getenv("RESEND_WEBHOOK_SECRET", "")
_SIMULATE_TOKEN        = os.getenv("SIMULATE_TOKEN", "")

logger = logging.getLogger(__name__)

# ── Rate limiter ──────────────────────────────────────────────────────────────

limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Tenacious Conversion Agent", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Webhook signature verification ───────────────────────────────────────────

def _verify_resend_signature(headers: dict, raw_body: bytes) -> bool:
    """
    Verify a Resend/Svix HMAC-SHA256 signature.
    Skipped (returns True) when RESEND_WEBHOOK_SECRET is not configured.
    """
    if not _RESEND_WEBHOOK_SECRET:
        return True

    msg_id     = headers.get("svix-id", "")
    timestamp  = headers.get("svix-timestamp", "")
    sig_header = headers.get("svix-signature", "")

    if not (msg_id and timestamp and sig_header):
        logger.warning("resend_webhook_missing_svix_headers")
        return False

    try:
        if abs(time.time() - int(timestamp)) > 300:
            logger.warning("resend_webhook_stale_timestamp")
            return False
    except ValueError:
        return False

    signed       = f"{msg_id}.{timestamp}.{raw_body.decode()}"
    secret_bytes = base64.b64decode(_RESEND_WEBHOOK_SECRET.removeprefix("whsec_"))
    expected     = base64.b64encode(
        hmac.new(secret_bytes, signed.encode(), hashlib.sha256).digest()
    ).decode()

    for sig in sig_header.split(" "):
        _, actual = sig.split(",", 1) if "," in sig else ("v1", sig)
        if hmac.compare_digest(expected, actual):
            return True

    logger.warning("resend_webhook_signature_mismatch")
    return False


def _verify_at_webhook(raw: dict) -> bool:
    """Verify Africa's Talking username in the webhook payload."""
    return raw.get("username") == _AT_USERNAME


def _check_simulate_auth(request: Request):
    """Return a 401/403 JSONResponse if the simulate token is missing or wrong."""
    if not _SIMULATE_TOKEN:
        return JSONResponse(
            {"error": "SIMULATE_TOKEN not configured — set this env var to enable /simulate"},
            status_code=403,
        )
    token = request.headers.get("X-Simulate-Token", "")
    if not hmac.compare_digest(token.encode(), _SIMULATE_TOKEN.encode()):
        return JSONResponse({"error": "invalid X-Simulate-Token"}, status_code=401)
    return None


# ── Background task error wrapper ─────────────────────────────────────────────

def _run_bg(fn, *args, **kwargs) -> None:
    """Run a background task and log any unhandled exception rather than dropping it."""
    try:
        fn(*args, **kwargs)
    except Exception as exc:
        logger.error(
            "background_task_failed fn=%s err=%r",
            getattr(fn, "__name__", repr(fn)), exc,
            exc_info=True,
        )


# ── pipeline helpers ──────────────────────────────────────────────────────────

def _run_full_pipeline(email: str, text: str) -> dict:
    """Enrich → compose → send → log.  Called on first contact (email only)."""
    trace_id = lf.log_trace("first_contact", {"email": email, "text": text}, None)

    profile  = enrich_mod.enrich(email)
    lf.log_span(trace_id, "enrich", {"email": email}, profile.__dict__)

    company_signals = signals_mod.research(profile.domain, trace_id)
    profile.signals_research = company_signals.to_dict()
    lf.log_span(trace_id, "signals_research", {"domain": profile.domain}, profile.signals_research)

    gap_brief   = gap_mod.generate_competitor_gap_brief(profile, trace_id)
    send_result = email_mod.compose_and_send(profile, trace_id)

    lead         = db.get_or_create(email)
    lead.status  = "outreach_sent"
    lead.profile = {**_dc.asdict(profile), "competitor_gap_brief": gap_brief}

    name_parts = email.split("@")[0].split(".")
    first      = name_parts[0].title()
    last       = name_parts[1].title() if len(name_parts) > 1 else ""

    contact_id = hs.upsert_contact(
        email             = email,
        first_name        = first,
        last_name         = last,
        company           = profile.company_name,
        segment_label     = email_mod.SEGMENT_LABELS[profile.segment],
        ai_maturity_score = profile.ai_maturity_score,
        booking_url       = "",
        enrichment_ts     = profile.enriched_at,
        trace_id          = trace_id,
    )
    lead.hubspot_contact_id = contact_id
    db.save_lead(lead)

    lf.log_trace("first_contact_complete", {"email": email}, send_result, session_id=lead.lead_id)
    return {
        "lead_id":              lead.lead_id,
        "status":               "outreach_sent",
        "send_result":          send_result,
        "segment":              email_mod.SEGMENT_LABELS[profile.segment],
        "ai_maturity_score":    profile.ai_maturity_score,
        "competitor_gap_brief": gap_brief,
    }


def _run_reply_pipeline(identifier: str, text: str) -> dict:
    """
    Handle a reply from email or SMS: generate response, qualify, book if ready.

    `identifier` is the email address for email replies or the phone-resolved
    email for SMS replies (see /webhooks/sms for resolution logic).
    """
    trace_id = lf.log_trace("reply", {"identifier": identifier, "text": text}, None)

    result = conv.handle_reply(identifier, text, trace_id)
    lead   = db.get_or_create(identifier)

    if result["qualified"] and not lead.booking_url:
        name_parts     = identifier.split("@")[0].split(".") if "@" in identifier else [identifier]
        name           = " ".join(p.title() for p in name_parts)
        booking_result = booking.book(identifier, name, trace_id, api_key=_CALCOM_API_KEY)

        if booking_result.ok:
            lead.booking_url = booking_result.data.get("booking_url", "")
        else:
            logger.error("booking_failed identifier=%s err=%s", identifier, booking_result.error)

        profile = lead.profile
        hs.upsert_contact(
            email             = identifier if "@" in identifier else profile.get("email", identifier),
            first_name        = name_parts[0].title(),
            last_name         = name_parts[1].title() if len(name_parts) > 1 else "",
            company           = profile.get("company_name", ""),
            segment_label     = email_mod.SEGMENT_LABELS.get(profile.get("segment", 0), "generic"),
            ai_maturity_score = profile.get("ai_maturity_score", 2),
            booking_url       = lead.booking_url,
            enrichment_ts     = profile.get("enriched_at", ""),
            trace_id          = trace_id,
        )

        if lead.phone and lead.booking_url and booking_result.ok:
            sms_confirmation = sms_mod.send_booking_confirmation_sms(
                phone         = lead.phone,
                booking_title = booking_result.data.get("title", "Discovery Call"),
                start_time    = booking_result.data.get("start", ""),
                booking_url   = lead.booking_url,
                trace_id      = trace_id,
            )
            result["sms_confirmation"] = sms_confirmation

        result["booking_url"]    = lead.booking_url
        result["booking_result"] = booking_result.to_dict()
        db.save_lead(lead)

    lf.log_trace("reply_complete", {"identifier": identifier}, result, session_id=lead.lead_id)
    return result


# ── routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    import httpx as _httpx
    checks: dict[str, str] = {}

    checks["database"]   = "ok" if db.ping() else "unreachable"
    checks["langfuse"]   = (
        "configured" if os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
        else "missing_credentials"
    )
    checks["resend"]     = "configured" if os.getenv("RESEND_API_KEY") else "missing_key"
    checks["openrouter"] = "configured" if os.getenv("OPENROUTER_API_KEY") else "missing_key"

    calcom_url = os.getenv("CALCOM_API_URL", "http://localhost:3000")
    try:
        _httpx.head(calcom_url, timeout=2)
        checks["calcom"] = "reachable"
    except Exception:
        checks["calcom"] = "unreachable"

    degraded = any(
        v in ("unreachable", "missing_key", "missing_credentials")
        for v in checks.values()
    )
    return JSONResponse(
        {"status": "degraded" if degraded else "ok", "checks": checks,
         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
        status_code=503 if degraded else 200,
    )


@app.post("/webhooks/email")
@limiter.limit("60/minute")
async def webhook_email(request: Request, background_tasks: BackgroundTasks):
    """
    Handle Resend webhook events.

    Resend sends a JSON body with a "type" field for delivery events:
      email.bounced    → route to bounce handler (suppress / retry)
      email.complained → route to complaint handler (suppress)
      email.delivered  → no action needed
      (no type field)  → treat as an inbound reply from the prospect
    """
    raw_body = await request.body()

    if not _verify_resend_signature(dict(request.headers), raw_body):
        return JSONResponse({"error": "invalid signature"}, status_code=401)

    try:
        import json as _json
        body = _json.loads(raw_body)
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    event_type = body.get("type", "")

    # ── Bounce event ──────────────────────────────────────────────────────────
    if event_type == "email.bounced":
        data        = body.get("data", {})
        to_list     = data.get("to", [])
        email       = to_list[0] if to_list else ""
        bounce_info = data.get("bounce", {})
        bounce_type = bounce_info.get("type", "soft")
        reason      = bounce_info.get("message", "")
        trace_id    = lf.log_trace("email_bounced", {"email": email, "type": bounce_type}, None)
        bounce_result = email_mod.handle_bounce(email, bounce_type, reason, trace_id)
        if email:
            hs.mark_bounced(email, bounce_type, trace_id)
            lead        = db.get_or_create(email)
            lead.status = "disqualified"
            db.save_lead(lead)
        return JSONResponse(bounce_result)

    # ── Complaint event ───────────────────────────────────────────────────────
    if event_type == "email.complained":
        data    = body.get("data", {})
        to_list = data.get("to", [])
        email   = to_list[0] if to_list else ""
        trace_id = lf.log_trace("email_complaint", {"email": email}, None)
        complaint_result = email_mod.handle_complaint(email, trace_id)
        if email:
            hs.mark_bounced(email, "complaint", trace_id)
            lead        = db.get_or_create(email)
            lead.status = "disqualified"
            db.save_lead(lead)
        return JSONResponse(complaint_result)

    # ── Inbound reply / new lead ──────────────────────────────────────────────
    email     = body.get("from") or body.get("email", "")
    text      = body.get("text") or body.get("body", "")
    thread_id = body.get("thread_id", "")

    if not email:
        return JSONResponse({"error": "missing email"}, status_code=400)

    lead = db.get_or_create(email)
    if lead.status == "new" or not thread_id:
        background_tasks.add_task(_run_bg, _run_full_pipeline, email, text)
    else:
        background_tasks.add_task(_run_bg, _run_reply_pipeline, email, text)

    return JSONResponse({"status": "accepted", "email": email}, status_code=202)


@app.post("/webhooks/sms")
@limiter.limit("60/minute")
async def webhook_sms(request: Request, background_tasks: BackgroundTasks):
    """
    Africa's Talking inbound SMS callback.

    AT sends application/x-www-form-urlencoded (not JSON).  This endpoint
    handles both content types for compatibility with local testing.

    Warm-lead gate (Layer 1)
    ------------------------
    1. Verify the AT username in the payload.
    2. Resolve the inbound phone number to an email-keyed lead via
       db.get_by_phone().  No match → cold contact → reject (email first).
    3. Delegate to sms_handler.handle_inbound_sms() which applies Layer 2.
    """
    content_type = request.headers.get("content-type", "")

    if "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
        form = await request.form()
        raw  = dict(form)
    else:
        raw = await request.json()

    if not _verify_at_webhook(raw):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    parsed = sms_mod.parse_at_payload(raw)
    phone  = parsed["phone"]
    text   = parsed["text"]

    if not phone:
        return JSONResponse({"error": "missing phone"}, status_code=400)

    trace_id  = lf.log_trace("sms_inbound", {"phone": phone, "text": text}, None)

    warm_lead = db.get_by_phone(phone)
    if warm_lead is None:
        lf.log_trace(
            "sms_cold_rejected",
            {"phone": phone, "reason": "no_warm_lead_for_phone"},
            None,
        )
        return JSONResponse(
            {
                "routed": False,
                "reason": "channel_hierarchy_gate",
                "detail": (
                    "No warm lead found for this phone number. "
                    "SMS is a warm-lead channel; first contact must be initiated via email."
                ),
            },
            status_code=200,
        )

    background_tasks.add_task(
        _run_bg,
        sms_mod.handle_inbound_sms,
        phone             = phone,
        text              = text,
        lead_status       = warm_lead.status,
        trace_id          = trace_id,
        reply_pipeline_fn = lambda _phone, _text: _run_reply_pipeline(warm_lead.email, _text),
    )

    return JSONResponse({"status": "accepted", "phone": phone}, status_code=202)


@app.post("/simulate")
@limiter.limit("10/minute")
async def simulate(request: Request):
    """Inject a synthetic lead for end-to-end testing. Requires X-Simulate-Token header."""
    auth_err = _check_simulate_auth(request)
    if auth_err:
        return auth_err
    body   = await request.json()
    email  = body.get("email", "prospect@example.com")
    text   = body.get("text", "Tell me more about your engineering teams.")
    result = await asyncio.to_thread(_run_full_pipeline, email, text)
    return JSONResponse(result)


@app.post("/simulate/sms")
@limiter.limit("10/minute")
async def simulate_sms(request: Request):
    """
    Simulate an inbound AT SMS from a warm lead for integration testing.
    Requires X-Simulate-Token header and the lead to already exist
    (via /simulate or /webhooks/email).
    """
    auth_err = _check_simulate_auth(request)
    if auth_err:
        return auth_err

    body  = await request.json()
    email = body.get("email", "")
    phone = body.get("phone", "")
    text  = body.get("text", "Yes, let's talk.")

    if not email or not phone:
        return JSONResponse({"error": "email and phone required"}, status_code=400)

    db.link_phone(email, phone)

    raw      = {"from": phone, "text": text, "to": "Sandbox", "username": _AT_USERNAME}
    parsed   = sms_mod.parse_at_payload(raw)
    trace_id = lf.log_trace("sms_simulate", {"email": email, "phone": phone}, None)

    warm_lead = db.get_by_phone(phone)
    if warm_lead is None:
        return JSONResponse({"error": "lead not found after link_phone"}, status_code=500)

    result = sms_mod.handle_inbound_sms(
        phone             = phone,
        text              = parsed["text"],
        lead_status       = warm_lead.status,
        trace_id          = trace_id,
        reply_pipeline_fn = lambda _p, _t: _run_reply_pipeline(warm_lead.email, _t),
    )
    return JSONResponse(result)


if __name__ == "__main__":
    import argparse
    from agent.enrichment_pipeline import get_company_contacts

    parser = argparse.ArgumentParser(description="Run conversion engine pipeline")
    parser.add_argument("--live_mode", action="store_true", help="Run with real email/booking APIs")
    parser.add_argument("--company_id", help="Crunchbase company ID (format: crunchbase:company-name)")
    parser.add_argument("email", nargs="?", help="Optional direct email target")
    args = parser.parse_args()

    if args.company_id:
        if not args.company_id.startswith("crunchbase:"):
            print("Error: Company ID must start with 'crunchbase:'")
            raise SystemExit(1)
        company_name  = args.company_id.split(":", 1)[1]
        contacts      = get_company_contacts(company_name)
        if not contacts:
            print(f"No contacts found for {company_name}")
            raise SystemExit(1)
        primary_email = next((c["email"] for c in contacts if c.get("email")), None)
        if not primary_email:
            print(f"No email found for {company_name}")
            raise SystemExit(1)
        target_email = primary_email
    elif args.email:
        target_email = args.email
    else:
        print("Error: Must provide either --company_id or email")
        raise SystemExit(1)

    if args.live_mode:
        print(f"Running LIVE pipeline for {target_email}")
        result = _run_full_pipeline(target_email, "Manual run triggered via CLI")
        print(f"Pipeline completed: {result}")
    else:
        print(f"Running in test mode for {target_email} (no actual emails/bookings will be sent)")
