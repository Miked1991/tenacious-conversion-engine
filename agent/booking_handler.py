"""
Books a discovery call via Cal.com REST API (self-hosted Docker instance).
"""

import os
import time
from datetime import datetime

import httpx
from dotenv import load_dotenv

from agent.langfuse_logger import log_span
from agent.retry import http_retry
from agent.tool_result import ToolResult

load_dotenv()

_CALCOM_URL     = os.getenv("CALCOM_API_URL", "http://localhost:3000")
_EVENT_SLUG     = os.getenv("CALCOM_EVENT_TYPE_SLUG", "discovery-call")
# FM-2: only offer slots during business hours (configurable, defaults 09:00–17:00 UTC)
_BIZ_HOUR_START = int(os.getenv("BIZ_HOUR_START", "9"))
_BIZ_HOUR_END   = int(os.getenv("BIZ_HOUR_END", "17"))


def _is_business_hours(iso_time: str) -> bool:
    """Return True when the slot falls within configured business hours (UTC)."""
    try:
        dt = datetime.fromisoformat(iso_time.replace("Z", "+00:00"))
        return _BIZ_HOUR_START <= dt.hour < _BIZ_HOUR_END
    except Exception:
        return False


@http_retry
def _calcom_get(url: str, params: dict, headers: dict, timeout: int) -> httpx.Response:
    return httpx.get(url, params=params, headers=headers, timeout=timeout)


@http_retry
def _calcom_post(url: str, headers: dict, json: dict, timeout: int) -> httpx.Response:
    return httpx.post(url, headers=headers, json=json, timeout=timeout)


def _next_available_slot(api_key: str, event_type_id: int) -> str | None:
    """Return the first business-hours slot available in the next 7 days."""
    start  = time.strftime("%Y-%m-%dT00:00:00Z", time.gmtime())
    end_ts = time.time() + 7 * 86400
    end    = time.strftime("%Y-%m-%dT23:59:59Z", time.gmtime(end_ts))
    try:
        resp = _calcom_get(
            f"{_CALCOM_URL}/api/v1/slots",
            params={"eventTypeId": event_type_id, "startTime": start, "endTime": end},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=15,
        )
        slots = resp.json().get("slots", {})
        for day_slots in slots.values():
            for slot in day_slots:
                if _is_business_hours(slot["time"]):
                    return slot["time"]
    except Exception:
        pass
    return None


def _get_event_type_id(api_key: str) -> int | None:
    try:
        resp = _calcom_get(
            f"{_CALCOM_URL}/api/v1/event-types",
            params={},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10,
        )
        for et in resp.json().get("event_types", []):
            if et.get("slug") == _EVENT_SLUG:
                return et["id"]
    except Exception:
        pass
    return None


def book(email: str, name: str, trace_id: str, api_key: str = "") -> ToolResult:
    """
    Book a discovery call for the prospect.
    Returns ToolResult with data={booking_url, slot} on success.
    Falls back gracefully if Cal.com is not reachable after retries.
    """
    event_type_id = _get_event_type_id(api_key) if api_key else None
    if event_type_id is None:
        result = ToolResult(ok=False, error="event type not found")
        log_span(trace_id, "book_call", {"email": email}, result.to_dict())
        return result

    slot = _next_available_slot(api_key, event_type_id)
    if not slot:
        result = ToolResult(ok=False, error="no slots available")
        log_span(trace_id, "book_call", {"email": email}, result.to_dict())
        return result

    payload = {
        "eventTypeId": event_type_id,
        "start": slot,
        "responses": {
            "email": email,
            "name": name or email.split("@")[0].title(),
            "location": {"optionValue": "", "value": "integrations:google:meet"},
        },
        "timeZone": "UTC",
        "language": "en",
        "metadata": {},
    }
    try:
        resp = _calcom_post(
            f"{_CALCOM_URL}/api/v1/bookings",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        data = resp.json()
        uid = data.get("uid", "")
        booking_url = f"{_CALCOM_URL}/booking/{uid}" if uid else ""
        result = ToolResult(ok=True, data={"booking_url": booking_url, "slot": slot})
    except Exception as exc:
        result = ToolResult(ok=False, error=str(exc))

    log_span(trace_id, "book_call", payload, result.to_dict())
    return result
