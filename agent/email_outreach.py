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

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "openai/gpt-4o-mini")
_RESEND_KEY = os.getenv("RESEND_API_KEY", "")
_FROM_EMAIL = os.getenv("RESEND_FROM_EMAIL", "Tenacious Outreach <onboarding@resend.dev>")  # Using Resend's default domain
_MOCK_LLM = os.getenv("MOCK_LLM", "false").lower() in ("1", "true", "yes")

# Segment-aware mock email templates used when MOCK_LLM=true
_MOCK_SUBJECTS = {
    1: "Faster hiring for your engineering push",
    2: "More output from your lean engineering team",
    3: "Scale your engineering team without the debt",
    0: "One question about your engineering challenges",
}
_MOCK_BODIES = {
    1: (
        "Congrats on the recent round — that kind of momentum usually means a sprint to ship.\n\n"
        "Most funded teams we talk to run into the same bottleneck: hiring takes six weeks and "
        "you needed the engineer yesterday.\n\n"
        "Tenacious places senior backend and frontend engineers from East Africa in two to three "
        "weeks, at 40–60% of US hiring cost, fully managed.\n\n"
        "Worth a 20-minute call to see if there's a fit?"
    ),
    2: (
        "Running lean after a tough quarter is hard — every hire has to count.\n\n"
        "Tenacious works with engineering leads who need output fast without rebuilding a full team. "
        "We place senior engineers from East Africa in two to three weeks, fully managed, "
        "at 40–60% of typical hiring cost.\n\n"
        "Would it help to see how other post-layoff teams have used this?"
    ),
    3: (
        "Scaling fast is exciting — until technical debt shows up uninvited.\n\n"
        "Tenacious places senior engineers who know how to build clean during a growth sprint. "
        "Two to three weeks to placement, 40–60% cost savings, fully managed engagement.\n\n"
        "Open to a quick conversation about what your team needs right now?"
    ),
    0: (
        "Quick question: what's the biggest friction point in your engineering team today — "
        "hiring speed, capacity, or something else?\n\n"
        "I ask because Tenacious helps engineering leads solve exactly these problems with "
        "senior talent from East Africa, placed in two to three weeks.\n\n"
        "Happy to share how if it's relevant."
    ),
}

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


def _deterministic_tone_check(subject: str, body: str) -> tuple[bool, list[str]]:
    """
    Fast pre-check before LLM tone validation.
    Returns (passed, violations_list).
    Catches: word count overrun, banned buzzwords, explicit 'AI' mention.
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

    return len(violations) == 0, violations


def _llm(messages: list, max_tokens: int = 400) -> str:
    if _MOCK_LLM:
        return "YES"   # default mock — callers that need compose output use _mock_compose
    try:
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


def _mock_compose(profile: "CompanyProfile") -> tuple[str, str]:
    subject = _MOCK_SUBJECTS.get(profile.segment, _MOCK_SUBJECTS[0])
    body = _MOCK_BODIES.get(profile.segment, _MOCK_BODIES[0])
    return subject, body


def compose(profile: CompanyProfile, trace_id: str) -> tuple[str, str]:
    """Return (subject, body) as plain text."""
    if _MOCK_LLM:
        subject, body = _mock_compose(profile)
        log_span(trace_id, "compose_email_mock", {}, {"subject": subject, "segment": profile.segment})
        return subject, body

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


# Segment label helper (used by HubSpot sync and main.py)
SEGMENT_LABELS: dict[int, str] = {
    0: "generic",
    1: "recently_funded",
    2: "post_layoff",
    3: "hypergrowth",
}
