"""
SMS channel handler — Africa's Talking bidirectional integration.

Warm-lead channel hierarchy (ENFORCED)
---------------------------------------
SMS is NEVER used for cold outreach.  The gate is checked at two layers:

  Layer 1 – main.py /webhooks/sms:
      Resolves the inbound phone number to an email-keyed lead via
      conversation_handler.get_by_phone().  If no matching warm lead exists
      the request is rejected before reaching this module.

  Layer 2 – handle_inbound_sms() below:
      Accepts a lead_status argument and returns routed=False with
      reason="channel_hierarchy_gate" for any status outside the warm set
      {"outreach_sent", "in_conversation", "qualified"}.

Outbound SMS (send_sms / send_booking_confirmation_sms) is called only from
_run_reply_pipeline in main.py, after a lead has been qualified and a Cal.com
booking has been confirmed — never on first contact.

Africa's Talking API notes
--------------------------
Outbound – POST https://api.africastalking.com/version1/messaging
           Content-Type: application/x-www-form-urlencoded
           Header: apiKey: <key>
           Body fields: username, to, message, from (sender ID)

Inbound  – Africa's Talking POSTs application/x-www-form-urlencoded to
           /webhooks/sms with fields:
             from        – sender MSISDN  e.g. "+254711XXXXXX"
             text        – message body
             to          – recipient shortcode / sender ID
             date        – delivery timestamp
             id          – AT message ID
             linkId      – for premium / subscription messages
             networkCode – carrier MCC+MNC code
"""

import os
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

from agent.langfuse_logger import log_span
from agent.retry import http_retry

load_dotenv()

_AT_USERNAME = os.getenv("AFRICA_TALKING_USERNAME", "sandbox")
_AT_API_KEY = os.getenv("AFRICA_TALKING_API_KEY", "")
_AT_SENDER_ID = os.getenv("AFRICA_TALKING_SENDER_ID", "Sandbox")

# Africa's Talking uses different hostnames for sandbox vs live traffic
_AT_BASE = (
    "https://api.sandbox.africastalking.com"
    if _AT_USERNAME == "sandbox"
    else "https://api.africastalking.com"
)
_AT_SMS_URL = f"{_AT_BASE}/version1/messaging"

# Lead statuses that indicate prior email contact (warm lead)
_WARM_STATUSES = {"outreach_sent", "in_conversation", "qualified"}


# ── Payload normalisation ────────────────────────────────────────────────────

def parse_at_payload(raw: dict) -> dict:
    """
    Normalise Africa's Talking inbound webhook fields to internal field names.

    AT sends application/x-www-form-urlencoded, not JSON.  Call this after
    FastAPI decodes the form data with ``await request.form()``.

    AT field → internal field
    -------------------------
    from        → phone
    text        → text
    to          → shortcode
    date        → at_date
    id          → at_message_id
    linkId      → at_link_id
    networkCode → network_code
    """
    return {
        "phone": raw.get("from", ""),
        "text": raw.get("text", ""),
        "shortcode": raw.get("to", ""),
        "at_date": raw.get("date", ""),
        "at_message_id": raw.get("id", ""),
        "at_link_id": raw.get("linkId", ""),
        "network_code": raw.get("networkCode", ""),
    }


# ── Outbound SMS ─────────────────────────────────────────────────────────────

@http_retry
def _at_post(payload: dict) -> httpx.Response:
    return httpx.post(
        _AT_SMS_URL,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "apiKey": _AT_API_KEY,
        },
        content=urlencode(payload).encode(),
        timeout=15,
    )


def send_sms(phone: str, message: str, trace_id: str = "") -> dict:
    """
    Send an outbound SMS via Africa's Talking.

    Only invoke for warm leads.  Cold-outreach initiation must use
    email_outreach.compose_and_send() instead.

    Uses application/x-www-form-urlencoded as required by the AT REST API.
    """
    payload: dict[str, str] = {
        "username": _AT_USERNAME,
        "to": phone,
        "message": message,
    }
    if _AT_SENDER_ID:
        payload["from"] = _AT_SENDER_ID

    try:
        resp   = _at_post(payload)
        result = resp.json()
    except Exception as exc:
        result = {"error": str(exc)}
    if trace_id:
        log_span(trace_id, "send_sms", {"phone": phone, "message": message[:60]}, result)
    return result


def send_booking_confirmation_sms(
    phone: str,
    booking_title: str,
    start_time: str,
    booking_url: str,
    trace_id: str = "",
) -> dict:
    """
    Send a Cal.com booking confirmation SMS to a warm lead's phone.
    Called from _run_reply_pipeline in main.py immediately after booking.
    """
    message = (
        f"Confirmed: {booking_title}. "
        f"Starts {start_time}. "
        f"Manage at {booking_url}"
    )
    return send_sms(phone, message, trace_id)


# ── Inbound routing ──────────────────────────────────────────────────────────

def handle_inbound_sms(
    phone: str,
    text: str,
    lead_status: str,
    trace_id: str,
    reply_pipeline_fn,
) -> dict:
    """
    Route an inbound AT SMS reply through the warm-lead channel hierarchy gate.

    Gate logic (Layer 2)
    --------------------
    lead_status must be in _WARM_STATUSES {"outreach_sent", "in_conversation",
    "qualified"}.  Status "new" means this phone number has no prior email
    contact and is refused — cold outreach is email-only.

    Warm leads are passed to reply_pipeline_fn (which is _run_reply_pipeline
    from main.py), so the same qualification scoring and Cal.com booking logic
    runs regardless of whether the reply arrived via email or SMS.

    Parameters
    ----------
    phone             : normalised sender MSISDN from parse_at_payload()
    text              : inbound message body
    lead_status       : current lead status resolved from email-keyed Lead
    trace_id          : Langfuse trace ID for span logging
    reply_pipeline_fn : _run_reply_pipeline(identifier, text) from main.py
    """
    # ── Layer 2 warm-lead gate ────────────────────────────────────────────────
    if lead_status not in _WARM_STATUSES:
        return {
            "routed": False,
            "reason": "channel_hierarchy_gate",
            "detail": (
                f"SMS is a warm-lead channel (requires prior email outreach). "
                f"Lead status '{lead_status}' is not in warm set {sorted(_WARM_STATUSES)}. "
                "Initiate first contact via email."
            ),
            "phone": phone,
        }

    # ── Warm lead — delegate to the shared downstream reply pipeline ──────────
    result = reply_pipeline_fn(phone, text)
    result["channel"] = "sms"
    result["phone"] = phone
    return {"routed": True, "result": result}
