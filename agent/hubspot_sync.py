"""
Creates / updates HubSpot contacts and logs engagement activities.
"""

import os
import time

import httpx
from dotenv import load_dotenv

from agent.langfuse_logger import log_span

load_dotenv()

_TOKEN = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
_BASE = "https://api.hubapi.com"

_HEADERS = {
    "Authorization": f"Bearer {_TOKEN}",
    "Content-Type": "application/json",
}


def _contact_id_by_email(email: str) -> str | None:
    try:
        resp = httpx.get(
            f"{_BASE}/crm/v3/objects/contacts/search",
            headers=_HEADERS,
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {"propertyName": "email", "operator": "EQ", "value": email}
                        ]
                    }
                ],
                "properties": ["email"],
                "limit": 1,
            },
            timeout=15,
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
        "segment__c": segment_label,                      # custom property
        "ai_maturity_score__c": str(ai_maturity_score),  # custom property
        "booking_url__c": booking_url,                    # custom property
        "enrichment_timestamp__c": enrichment_ts,         # custom property
    }

    existing_id = _contact_id_by_email(email)
    try:
        if existing_id:
            resp = httpx.patch(
                f"{_BASE}/crm/v3/objects/contacts/{existing_id}",
                headers=_HEADERS,
                json={"properties": props},
                timeout=15,
            )
            contact_id = resp.json().get("id", existing_id)
        else:
            resp = httpx.post(
                f"{_BASE}/crm/v3/objects/contacts",
                headers=_HEADERS,
                json={"properties": props},
                timeout=15,
            )
            contact_id = resp.json().get("id", "")
    except Exception as exc:
        contact_id = ""
        log_span(trace_id, "hubspot_upsert_error", props, str(exc), level="ERROR")
        return contact_id

    log_span(trace_id, "hubspot_upsert", props, {"contact_id": contact_id})
    return contact_id


def log_email_activity(contact_id: str, subject: str, body: str, trace_id: str) -> None:
    if not contact_id:
        return
    payload = {
        "engagement": {
            "active": True,
            "type": "EMAIL",
            "timestamp": int(time.time() * 1000),
        },
        "associations": {"contactIds": [int(contact_id)]},
        "metadata": {"subject": subject, "text": body},
    }
    try:
        httpx.post(
            f"{_BASE}/engagements/v1/engagements",
            headers=_HEADERS,
            json=payload,
            timeout=15,
        )
    except Exception:
        pass
    log_span(trace_id, "hubspot_log_email", payload, None)
