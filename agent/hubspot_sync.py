"""
Creates / updates HubSpot contacts and logs engagement activities.
"""

import os
import time

import httpx
from dotenv import load_dotenv

from agent.langfuse_logger import log_span
from agent.retry import http_retry

load_dotenv()

_BASE = "https://api.hubapi.com"


def _headers() -> dict:
    """Build headers fresh each call so token rotation takes effect without restart."""
    return {
        "Authorization": f"Bearer {os.getenv('HUBSPOT_ACCESS_TOKEN', '')}",
        "Content-Type": "application/json",
    }


@http_retry
def _hs_search(payload: dict) -> httpx.Response:
    return httpx.post(
        f"{_BASE}/crm/v3/objects/contacts/search",
        headers=_headers(),
        json=payload,
        timeout=15,
    )


@http_retry
def _hs_create(payload: dict) -> httpx.Response:
    return httpx.post(
        f"{_BASE}/crm/v3/objects/contacts",
        headers=_headers(),
        json=payload,
        timeout=15,
    )


@http_retry
def _hs_patch(contact_id: str, payload: dict) -> httpx.Response:
    return httpx.patch(
        f"{_BASE}/crm/v3/objects/contacts/{contact_id}",
        headers=_headers(),
        json=payload,
        timeout=15,
    )


@http_retry
def _hs_engage(payload: dict) -> httpx.Response:
    return httpx.post(
        f"{_BASE}/engagements/v1/engagements",
        headers=_headers(),
        json=payload,
        timeout=15,
    )


def _contact_id_by_email(email: str) -> str | None:
    try:
        resp = _hs_search(
            {
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": ["email"],
                "limit": 1,
            }
        )
        results = resp.json().get("results", [])
        return results[0]["id"] if results else None
    except Exception:
        return None


def upsert_contact(
    email: str,
    first_name: str,
    last_name: str,
    company: str,
    segment_label: str,
    ai_maturity_score: int,
    booking_url: str,
    enrichment_ts: str,
    trace_id: str,
) -> str:
    """Create or update a HubSpot contact. Returns the contact ID."""
    props = {
        "email": email,
        "firstname": first_name,
        "lastname": last_name,
        "company": company,
        "hs_lead_status": "IN_PROGRESS",
        "message": (
            f"segment={segment_label} | "
            f"ai_maturity={ai_maturity_score} | "
            f"enriched={enrichment_ts} | "
            f"booking={booking_url}"
        ),
    }

    existing_id = _contact_id_by_email(email)
    try:
        if existing_id:
            resp = _hs_patch(existing_id, {"properties": props})
            contact_id = resp.json().get("id", existing_id)
        else:
            resp = _hs_create({"properties": props})
            # 409 = contact exists but search missed it (eventual consistency)
            if resp.status_code == 409:
                msg   = resp.json().get("message", "")
                parts = msg.split("Existing ID: ")
                conflict_id = parts[-1].strip() if len(parts) > 1 else ""
                if conflict_id and conflict_id.isdigit():
                    resp = _hs_patch(conflict_id, {"properties": props})
                    contact_id = resp.json().get("id", conflict_id)
                else:
                    log_span(
                        trace_id, "hubspot_409_unparseable",
                        {"email": email, "msg": msg}, {}, level="ERROR"
                    )
                    contact_id = ""
            else:
                contact_id = resp.json().get("id", "")
    except Exception as exc:
        contact_id = ""
        log_span(trace_id, "hubspot_upsert_error", props, str(exc), level="ERROR")
        return contact_id

    log_span(trace_id, "hubspot_upsert", props, {"contact_id": contact_id})
    return contact_id


def mark_bounced(email: str, bounce_type: str, trace_id: str) -> None:
    """
    Update the HubSpot lead status when Resend reports a bounce or complaint.

    hard / complaint → hs_lead_status = UNQUALIFIED (suppress permanently)
    soft             → hs_lead_status = ATTEMPTED_TO_CONTACT (allow retry)
    """
    status_map = {
        "hard":      "UNQUALIFIED",
        "complaint": "UNQUALIFIED",
        "soft":      "ATTEMPTED_TO_CONTACT",
    }
    hs_status  = status_map.get(bounce_type, "ATTEMPTED_TO_CONTACT")
    contact_id = _contact_id_by_email(email)
    if not contact_id:
        return
    try:
        _hs_patch(contact_id, {"properties": {"hs_lead_status": hs_status}})
    except Exception as exc:
        log_span(
            trace_id, "hubspot_mark_bounced_error",
            {"email": email, "bounce_type": bounce_type}, str(exc), level="ERROR"
        )
        return
    log_span(
        trace_id, "hubspot_mark_bounced",
        {"email": email, "bounce_type": bounce_type}, {"hs_lead_status": hs_status}
    )


def log_email_activity(contact_id: str, subject: str, body: str, trace_id: str) -> None:
    if not contact_id:
        return
    payload = {
        "engagement": {
            "active":    True,
            "type":      "EMAIL",
            "timestamp": int(time.time() * 1000),
        },
        "associations": {"contactIds": [int(contact_id)]},
        "metadata":     {"subject": subject, "text": body},
    }
    try:
        _hs_engage(payload)
    except Exception as exc:
        log_span(trace_id, "hubspot_log_email_error", payload, str(exc), level="ERROR")
        return
    log_span(trace_id, "hubspot_log_email", payload, None)
