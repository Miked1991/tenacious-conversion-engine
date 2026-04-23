"""
FastAPI orchestrator – the single entry point for all incoming webhooks.

Endpoints
---------
POST /webhooks/email   – incoming email (Resend webhook or simulation)
POST /webhooks/sms     – incoming SMS (Africa's Talking webhook)
POST /simulate         – synthetic lead injection for testing
GET  /health           – liveness check
"""

import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

load_dotenv()

from agent import enrichment_pipeline as enrich_mod
from agent import email_outreach as email_mod
from agent import conversation_handler as conv
from agent import booking_handler as booking
from agent import hubspot_sync as hs
from agent import langfuse_logger as lf

_CALCOM_API_KEY = os.getenv("CALCOM_API_KEY", "")   # set if Cal.com auth is required


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield


app = FastAPI(title="Tenacious Conversion Agent", lifespan=lifespan)


# ── helpers ──────────────────────────────────────────────────────────────────

def _run_full_pipeline(email: str, text: str) -> dict:
    """Enrich → compose → send → log.  Called on first contact."""
    trace_id = lf.log_trace("first_contact", {"email": email, "text": text}, None)

    profile = enrich_mod.enrich(email)
    lf.log_span(trace_id, "enrich", {"email": email}, profile.__dict__)

    send_result = email_mod.compose_and_send(profile, trace_id)

    lead = conv.get_or_create(email)
    lead.status = "outreach_sent"
    lead.profile = profile.__dict__

    name_parts = email.split("@")[0].split(".")
    first = name_parts[0].title()
    last = name_parts[1].title() if len(name_parts) > 1 else ""

    contact_id = hs.upsert_contact(
        email=email,
        first_name=first,
        last_name=last,
        company=profile.company_name,
        segment_label=email_mod.SEGMENT_LABELS[profile.segment],
        ai_maturity_score=profile.ai_maturity_score,
        booking_url="",
        enrichment_ts=profile.enriched_at,
        trace_id=trace_id,
    )
    lead.hubspot_contact_id = contact_id

    lf.log_trace("first_contact_complete", {"email": email}, send_result, session_id=lead.lead_id)
    return {"lead_id": lead.lead_id, "status": "outreach_sent", "send_result": send_result}


def _run_reply_pipeline(email: str, text: str) -> dict:
    """Handle a reply: generate response, qualify, book if ready."""
    trace_id = lf.log_trace("reply", {"email": email, "text": text}, None)

    result = conv.handle_reply(email, text, trace_id)
    lead = conv.get_or_create(email)

    if result["qualified"] and not lead.booking_url:
        name_parts = email.split("@")[0].split(".")
        name = " ".join(p.title() for p in name_parts)
        booking_result = booking.book(email, name, trace_id, api_key=_CALCOM_API_KEY)
        lead.booking_url = booking_result.get("booking_url", "")

        # update HubSpot with booking URL
        profile = lead.profile
        hs.upsert_contact(
            email=email,
            first_name=name_parts[0].title(),
            last_name=name_parts[1].title() if len(name_parts) > 1 else "",
            company=profile.get("company_name", ""),
            segment_label=email_mod.SEGMENT_LABELS.get(profile.get("segment", 0), "generic"),
            ai_maturity_score=profile.get("ai_maturity_score", 2),
            booking_url=lead.booking_url,
            enrichment_ts=profile.get("enriched_at", ""),
            trace_id=trace_id,
        )
        result["booking_url"] = lead.booking_url
        result["booking_result"] = booking_result

    lf.log_trace("reply_complete", {"email": email}, result, session_id=lead.lead_id)
    return result


# ── routes ───────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}


@app.post("/webhooks/email")
async def webhook_email(request: Request):
    body = await request.json()
    email = body.get("from") or body.get("email", "")
    text = body.get("text") or body.get("body", "")
    thread_id = body.get("thread_id", "")

    if not email:
        return JSONResponse({"error": "missing email"}, status_code=400)

    lead = conv.get_or_create(email)
    if lead.status == "new" or not thread_id:
        result = _run_full_pipeline(email, text)
    else:
        result = _run_reply_pipeline(email, text)

    return JSONResponse(result)


@app.post("/webhooks/sms")
async def webhook_sms(request: Request):
    body = await request.json()
    # Africa's Talking sends: {"from": "+2547...", "text": "...", ...}
    phone = body.get("from", "")
    text = body.get("text", "")

    # Use phone as the identifier; no enrichment for SMS-only
    lead = conv.get_or_create(phone)
    trace_id = lf.log_trace("sms_reply", {"phone": phone, "text": text}, None)
    agent_reply = conv._llm_reply(lead.history + [{"role": "user", "content": text}], trace_id)
    lead.history.append({"role": "user", "content": text})
    lead.history.append({"role": "assistant", "content": agent_reply})

    lf.log_trace("sms_complete", {"phone": phone}, agent_reply, session_id=lead.lead_id)
    return JSONResponse({"reply": agent_reply, "lead_id": lead.lead_id})


@app.post("/simulate")
async def simulate(request: Request):
    """Inject a synthetic lead for end-to-end testing."""
    body = await request.json()
    email = body.get("email", "prospect@example.com")
    text = body.get("text", "Tell me more about your engineering teams.")
    result = _run_full_pipeline(email, text)
    return JSONResponse(result)
