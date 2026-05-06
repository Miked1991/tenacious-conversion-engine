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
import re
from typing import Literal

import httpx
from dotenv import load_dotenv

from agent.enrichment_pipeline import CompanyProfile
from agent.langfuse_logger import log_span
from agent.retry import http_retry

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "openai/gpt-4o-mini")
_RESEND_KEY = os.getenv("RESEND_API_KEY", "")
_FROM_EMAIL  = os.getenv("RESEND_FROM_EMAIL", "Tenacious Outreach <onboarding@resend.dev>")
_REPLY_TO    = os.getenv("RESEND_REPLY_TO", "")

# Kill-switch (data policy rule 4): default OFF — routes all outbound to staff sink.
# Set OUTBOUND_LIVE=true only after Tenacious executive team approval.
_OUTBOUND_LIVE = os.getenv("OUTBOUND_LIVE", "false").lower() in ("1", "true", "yes")
_STAFF_SINK = os.getenv("STAFF_SINK_EMAIL", "sink@tenacious-pilot.dev")

# Startup guard: require explicit approval evidence before going live.
# Prevents accidental live sends if OUTBOUND_LIVE leaks into staging.
if _OUTBOUND_LIVE and not os.getenv("OUTBOUND_LIVE_APPROVED_BY"):
    raise RuntimeError(
        "OUTBOUND_LIVE=true requires OUTBOUND_LIVE_APPROVED_BY env var "
        "(e.g. 'cto@tenacious.io 2026-05-06') for audit trail. "
        "Set both in your deployment config before going live."
    )

BounceType = Literal["hard", "soft", "complaint"]

_SEGMENT_HINTS = {
    1: (
        "Recently funded (Series A/B). Fresh budget, runway clock ticking. "
        "Lead with hiring velocity: how we cut time-to-hire and let them ship faster."
    ),
    2: (
        "Post-layoff or mid-market restructuring — running lean under board pressure. "
        "Lead with cost efficiency: same output, lower burn, no compromise on delivery quality."
    ),
    3: (
        "New CTO or VP Engineering appointed in last 90 days — vendor-reassessment window. "
        "Lead with a clean, practical partnership offer: fast ramp, transparent model, no lock-in."
    ),
    4: (
        "Active AI/ML capability gap (maturity score ≥ 2). "
        "Lead with Tenacious AI/data engineers for ML platform migration or agentic system build. "
        "Frame as project-based consulting: defined scope, faster than hiring, higher leverage."
    ),
    0: (
        "Generic exploratory outreach. "
        "Ask one warm, diagnostic question about their current engineering challenge."
    ),
}

_STYLE_GUIDE = (
    "Tenacious style: warm, direct, human. No jargon. No buzzwords. "
    "Short sentences. One clear call to action. Under 120 words. "
    "Never mention 'AI' or 'disruption'."
)

# ── Deterministic tone guards (run before LLM tone-check) ────────────────────

_BANNED_WORDS = re.compile(
    r"\b(disrupt|disruption|leverage|synergy|artificial intelligence|"
    r"machine learning|innovative|innovation|revolutionize|game.?changer|"
    r"paradigm|bleeding.?edge|world.?class|best.?in.?class|cutting.?edge|"
    r"transformational|empower|unlock|scalable solution|robust solution)\b",
    re.IGNORECASE,
)
_AI_WORD = re.compile(r"\bAI\b")          # case-sensitive: "AI" is banned, "ai" in names is ok
_MAX_BODY_WORDS = 120
# FM-1: catch commitment language that books / schedules before the prospect consents
_COMMITMENT_WORDS = re.compile(
    r"\b(i(?:'ve| have) (?:booked|scheduled|reserved|set up|blocked)|"
    r"(?:booked|scheduled|reserved) you|your (?:slot|booking|appointment|calendar invite)|"
    r"confirmed for|i(?:'ll| will) (?:send|shoot) (?:you )?a (?:calendar|cal) invite|"
    r"expect (?:a )?(?:calendar|meeting|invite) from me)\b",
    re.IGNORECASE,
)


def _deterministic_tone_check(subject: str, body: str) -> tuple[bool, list[str]]:
    """
    Fast pre-check before LLM tone validation.
    Returns (passed, violations_list).
    Catches: word count overrun, banned buzzwords, explicit 'AI' mention,
    and FM-1 commitment language that books before consent.
    """
    violations: list[str] = []
    full_text = f"{subject} {body}"

    word_count = len(body.split())
    if word_count > _MAX_BODY_WORDS:
        violations.append(f"body too long ({word_count} words; max {_MAX_BODY_WORDS})")

    banned = _BANNED_WORDS.findall(full_text)
    if banned:
        violations.append(
            f"banned buzzwords: {', '.join(sorted(set(w.lower() for w in banned)))}"
        )

    if _AI_WORD.search(full_text):
        violations.append("contains 'AI' (style guide: never mention AI)")

    # FM-1: email must ask for permission, never commit to a specific booking
    commit_hits = _COMMITMENT_WORDS.findall(full_text)
    if commit_hits:
        violations.append(
            f"commits to booking before consent: {', '.join(sorted(set(h.lower() for h in commit_hits)))}"
        )

    return len(violations) == 0, violations


@http_retry
def _llm_post(messages: list, max_tokens: int) -> httpx.Response:
    return httpx.post(
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


def _llm(messages: list, max_tokens: int = 400) -> str:
    try:
        resp = _llm_post(messages, max_tokens)
        data = resp.json()
        msg = data["choices"][0]["message"]
        content = msg.get("content") or ""
        # Strip in-band thinking tokens (Qwen3 / o-series style)
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        if not content:
            # Fall back to reasoning field if content is empty after stripping
            content = (msg.get("reasoning") or "").strip()
        if not content:
            raise RuntimeError(
                f"LLM returned empty content. Full response: {data}"
            )
        return content
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"LLM call failed ({_DEV_MODEL}): {exc}") from exc


def compose(profile: CompanyProfile, trace_id: str) -> tuple[str, str]:
    """Return (subject, body) as plain text."""
    hint = _SEGMENT_HINTS.get(profile.segment, _SEGMENT_HINTS[0])

    # Signal-confidence-aware phrasing (per ICP spec: "ask rather than assert" when weak)
    job_conf = (profile.job_posts_signal or {}).get("confidence", 0.5)
    cb_conf  = (profile.crunchbase_signal or {}).get("confidence", 0.5)
    avg_conf = (job_conf + cb_conf) / 2
    if avg_conf >= 0.7:
        confidence_note = (
            "Signal confidence is HIGH — you may make specific, grounded claims "
            "(e.g. exact funding stage, number of open roles)."
        )
    elif avg_conf >= 0.4:
        confidence_note = (
            "Signal confidence is MEDIUM — use hedged language "
            "(e.g. 'it looks like', 'based on what we can see publicly')."
        )
    else:
        confidence_note = (
            "Signal confidence is LOW — ask rather than assert; "
            "use exploratory, curiosity-driven language throughout."
        )

    personalization = (profile.signals_research or {}).get("personalization_hook", "")
    personalization_line = (
        f"Personalization hook (weave in naturally if relevant, don't force it): {personalization}\n"
        if personalization else ""
    )
    prompt = (
        f"Write a cold outreach email to {profile.company_name} ({profile.domain}).\n"
        f"Segment context: {hint}\n"
        f"Signal confidence guidance: {confidence_note}\n"
        f"{personalization_line}"
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


@http_retry
def _resend_post(payload: dict) -> httpx.Response:
    return httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {_RESEND_KEY}"},
        json=payload,
        timeout=20,
    )


def send(to: str, subject: str, body: str, trace_id: str) -> dict:
    """Send via Resend. Routes to staff sink unless OUTBOUND_LIVE=true."""
    actual_to = to if _OUTBOUND_LIVE else _STAFF_SINK
    payload = {
        "from": _FROM_EMAIL,
        "to": [actual_to],
        "subject": subject,
        "text": body,
        "tags": [{"name": "draft", "value": "true"}],
    }
    if _REPLY_TO:
        payload["reply_to"] = [_REPLY_TO]
    if not _OUTBOUND_LIVE:
        payload["subject"] = f"[SINK:{to}] {subject}"
    try:
        resp = _resend_post(payload)
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


def compose_and_send(profile: CompanyProfile, trace_id: str, dry_run: bool = False) -> dict:
    """
    Compose → deterministic guard → LLM tone check → send.
    Max 2 compose attempts total.

    dry_run=True skips the Resend API call (returns a simulated success dict).
    Used by the batch CSV runner to avoid sending real emails to CSV contacts.
    """
    subject, body = compose(profile, trace_id)

    # Gate 1: fast deterministic check (word count, banned words, AI mention)
    det_ok, violations = _deterministic_tone_check(subject, body)
    if not det_ok:
        log_span(
            trace_id, "tone_check_deterministic_fail",
            {"violations": violations, "retrying": True}, {}
        )
        subject, body = compose(profile, trace_id)   # retry

    # Gate 2: LLM tone check on the (possibly retried) output
    if not tone_check(subject, body, trace_id):
        log_span(trace_id, "tone_check_llm_fail", {"subject": subject[:60]}, {})
        # Do not retry again — max 2 compose calls total; proceed with current output

    if dry_run:
        result = {
            "id": f"dry-run-{trace_id}",
            "dry_run": True,
            "subject": subject,
            "body": body,
            "to": profile.email,
        }
        log_span(trace_id, "send_email_dry_run", {"to": profile.email}, result)
        return result

    return send(profile.email, subject, body, trace_id)


# Segment label helper — names are fixed for grading per challenge ICP spec
SEGMENT_LABELS: dict[int, str] = {
    0: "generic",
    1: "recently_funded",
    2: "restructuring_cost",
    3: "leadership_transition",
    4: "capability_gap",
}
