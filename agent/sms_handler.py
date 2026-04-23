"""
SMS channel handler — Africa's Talking bidirectional integration.

Channel hierarchy (ENFORCED)
-----------------------------
SMS is a WARM-LEAD channel only.  The rule is applied at the routing layer:

  Cold leads  (status == "new", never received email outreach)
      → SMS refused; cold outreach is email-only.  A reason code is returned
        so the caller can log the event without further action.

  Warm leads  (status in ["outreach_sent", "in_conversation", "qualified"])
      → inbound SMS is accepted and routed to conversation_handler.handle_reply()
        via the reply_pipeline_fn callback, running the same qualification and
        booking logic as email replies.

Outbound SMS via send_sms() is used only for warm-lead follow-ups (e.g. booking
confirmation delivery or a nudge after a qualified lead goes quiet) — never for
first-contact cold outreach.

Africa's Talking API
---------------------
Outbound: POST https://api.africastalking.com/version1/messaging  (live)
          POST https://api.sandbox.africastalking.com/version1/messaging  (sandbox)
Inbound:  HTTP callback to /webhooks/sms (configured in AT dashboard)
          Payload: {"from": "+2547...", "text": "...", "to": "SENDER_ID", ...}
"""

import os
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

load_dotenv()

_AT_USERNAME = os.getenv("AFRICA_TALKING_USERNAME", "sandbox")
_AT_API_KEY = os.getenv("AFRICA_TALKING_API_KEY", "")
_AT_SENDER_ID = os.getenv("AFRICA_TALKING_SENDER_ID", "Sandbox")

# Sandbox vs live endpoint chosen based on username
_AT_BASE = (
    "https://api.sandbox.africastalking.com"
    if _AT_USERNAME == "sandbox"
    else "https://api.africastalking.com"
)
_AT_SMS_URL = f"{_AT_BASE}/version1/messaging"

# Lead statuses that indicate prior email contact (warm)
_WARM_STATUSES = {"outreach_sent", "in_conversation", "qualified"}


def send_sms(phone: str, message: str) -> dict:
    """
    Send an outbound SMS to `phone` via Africa's Talking.

    Only invoke this for warm leads.  Cold-outreach initiation must go through
    email_outreach.compose_and_send() instead.
    """
    payload = {
        "username": _AT_USERNAME,
        "to": phone,
        "message": message,
    }
    if _AT_SENDER_ID:
        payload["from"] = _AT_SENDER_ID
    try:
        resp = httpx.post(
            _AT_SMS_URL,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "apiKey": _AT_API_KEY,
            },
            content=urlencode(payload).encode(),
            timeout=15,
        )
        return resp.json()
    except Exception as exc:
        return {"error": str(exc)}


def handle_inbound_sms(
    phone: str,
    text: str,
    lead_status: str,
    trace_id: str,
    reply_pipeline_fn,
) -> dict:
    """
    Route an inbound SMS reply through the channel hierarchy gate.

    Parameters
    ----------
    phone             : sender phone number (Africa's Talking "from" field)
    text              : inbound message body
    lead_status       : current lead status from conversation_handler
    trace_id          : Langfuse trace ID
    reply_pipeline_fn : _run_reply_pipeline(identifier, text) from main.py —
                        the shared downstream handler for both email and SMS replies

    Returns
    -------
    dict with "routed" bool and either a "reason" (cold gate) or "result"
    (downstream pipeline output).
    """
    # ── Channel hierarchy gate ────────────────────────────────────────────────
    # SMS is only allowed for leads that have already been contacted by email.
    # "new" means no outreach has been sent yet → refuse SMS, enforce email first.
    if lead_status not in _WARM_STATUSES:
        return {
            "routed": False,
            "reason": "channel_hierarchy_gate",
            "detail": (
                "SMS is a warm-lead channel. This contact has not yet received "
                "email outreach. Initiate contact via email first."
            ),
            "phone": phone,
            "lead_status": lead_status,
        }

    # ── Warm lead: route to the shared downstream reply pipeline ─────────────
    # reply_pipeline_fn runs conversation_handler.handle_reply() which handles
    # qualification scoring and Cal.com booking identically for both channels.
    result = reply_pipeline_fn(phone, text)
    result["channel"] = "sms"
    result["phone"] = phone
    return {"routed": True, "result": result}
