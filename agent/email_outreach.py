"""
Composes segment-aware outreach emails, validates tone, sends via Resend,
and handles bounce / complaint events from the Resend webhook.

Public interface
----------------
compose(profile, trace_id)             → (subject, body)
tone_check(subject, body, trace_id)    → bool
send(to, subject, body, trace_id)      → dict  (Resend API response)
compose_and_send(profile, trace_id)    → dict
handle_bounce(email, bounce_type, reason, trace_id)  → dict
handle_complaint(email, trace_id)      → dict
"""

import os
from typing import Literal

import httpx
from dotenv import load_dotenv

from agent.enrichment_pipeline import CompanyProfile
from agent.langfuse_logger import log_span

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-next-80b-a3b-instruct")
_RESEND_KEY = os.getenv("RESEND_API_KEY", "")
_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "Tenacious Outreach <onboarding@resend.dev>")

BounceType = Literal["hard", "soft", "complaint"]

_SEGMENT_HINTS = {
    1: (
        "They recently secured funding and are aggressively hiring engineers. "
        "Lead with how we accelerate engineering velocity and reduce time-to-hire."
    ),
    2: (
        "They went through layoffs and are running lean. "
        "Lead with cost efficiency, doing more with less, and preserving institutional knowledge."
    ),
    3: (
        "They are in hyper-growth mode. "
        "Lead with scalability, avoiding technical debt during rapid expansion."
    ),
    0: (
        "Use a warm, curiosity-driven opener. "
        "Ask one diagnostic question about their current engineering challenges."
    ),
}

_STYLE_GUIDE = (
    "Tenacious style: warm, direct, human. No jargon. No buzzwords. "
    "Short sentences. One clear call to action. Under 120 words. "
    "Never mention 'AI' or 'disruption'."
)


def _llm(messages: list, max_tokens: int = 400) -> str:
    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {_OR_KEY}"},
        json={
            "model": _DEV_MODEL,
            "messages": messages,
            "temperature": float(os.getenv("TEMPERATURE", "0.7")),
            "max_tokens": max_tokens,
        },
        timeout=40,
    )
    return resp.json()["choices"][0]["message"]["content"].strip()


def compose(profile: CompanyProfile, trace_id: str) -> tuple[str, str]:
    """Return (subject, body) as plain text."""
    hint = _SEGMENT_HINTS.get(profile.segment, _SEGMENT_HINTS[0])
    prompt = (
        f"Write a cold outreach email to {profile.company_name} ({profile.domain}).\n"
        f"Context: {hint}\n"
        f"Style guide: {_STYLE_GUIDE}\n"
        "Return ONLY: subject line on the first line, then a blank line, then the body. "
        "No salutation line required."
    )
    raw = _llm([{"role": "user", "content": prompt}])
    lines = raw.split("\n")
    subject = lines[0].removeprefix("Subject:").strip()
    body = "\n".join(lines[2:]).strip() if len(lines) > 2 else raw
    log_span(trace_id, "compose_email", prompt, raw, {"segment": profile.segment})
    return subject, body


def tone_check(subject: str, body: str, trace_id: str) -> bool:
    """Return True when the email passes the style guide."""
    verdict = _llm(
        [
            {
                "role": "user",
                "content": (
                    f"Style guide: {_STYLE_GUIDE}\n\n"
                    f"Email subject: {subject}\nEmail body: {body}\n\n"
                    "Does this email comply with the style guide? "
                    "Reply with exactly one word: YES or NO."
                ),
            }
        ],
        max_tokens=5,
    )
    ok = "YES" in verdict.upper()
    log_span(trace_id, "tone_check", f"{subject}\n{body}", verdict)
    return ok


def send(to: str, subject: str, body: str, trace_id: str) -> dict:
    """Send via Resend. Returns the Resend API response dict."""
    payload = {
        "from": _FROM_EMAIL,
        "to": [to],
        "subject": subject,
        "text": body,
    }
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {_RESEND_KEY}"},
            json=payload,
            timeout=20,
        )
        result = resp.json()
    except Exception as exc:
        result = {"error": str(exc)}
    log_span(trace_id, "send_email", payload, result)
    return result


def handle_bounce(
    email: str,
    bounce_type: BounceType,
    reason: str,
    trace_id: str,
) -> dict:
    """
    Process a Resend email.bounced webhook event.

    bounce_type
    -----------
    "hard"      – permanent delivery failure (bad address, domain doesn't exist).
                  Mark the lead uncontactable; do not retry.
    "soft"      – transient failure (mailbox full, server timeout).
                  Eligible for one retry after 24 h.
    "complaint" – recipient marked as spam.
                  Suppress immediately; no further contact.

    Returns a structured dict so the orchestrator can update HubSpot accordingly.
    """
    should_suppress = bounce_type in ("hard", "complaint")
    result = {
        "email": email,
        "bounce_type": bounce_type,
        "reason": reason,
        "suppressed": should_suppress,
        "retry_eligible": bounce_type == "soft",
    }
    log_span(trace_id, "email_bounce", {"email": email, "type": bounce_type}, result)
    return result


def handle_complaint(email: str, trace_id: str) -> dict:
    """Process a Resend email.complained event — treat as hard suppress."""
    return handle_bounce(email, "complaint", "spam complaint", trace_id)


def compose_and_send(profile: CompanyProfile, trace_id: str) -> dict:
    """Compose, tone-check (one retry), and send. Returns Resend API response."""
    subject, body = compose(profile, trace_id)
    if not tone_check(subject, body, trace_id):
        subject, body = compose(profile, trace_id)   # one retry on tone failure
    return send(profile.email, subject, body, trace_id)


# Segment label helper (used by HubSpot sync and main.py)
SEGMENT_LABELS: dict[int, str] = {
    0: "generic",
    1: "recently_funded",
    2: "post_layoff",
    3: "hypergrowth",
}
