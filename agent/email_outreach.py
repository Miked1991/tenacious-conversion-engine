"""
Composes segment-aware outreach emails, validates tone, and sends via Resend.
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
    """Return (subject, body). Both are plain text."""
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


def _tone_check(subject: str, body: str, trace_id: str) -> bool:
    """Returns True when the email passes the style guide."""
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


def compose_and_send(profile: CompanyProfile, trace_id: str) -> dict:
    subject, body = compose(profile, trace_id)
    if not _tone_check(subject, body, trace_id):
        subject, body = compose(profile, trace_id)   # one retry
    return send(profile.email, subject, body, trace_id)


# Segment label helper (used by HubSpot sync)
SEGMENT_LABELS: dict[int, str] = {
    0: "generic",
    1: "recently_funded",
    2: "post_layoff",
    3: "hypergrowth",
}
