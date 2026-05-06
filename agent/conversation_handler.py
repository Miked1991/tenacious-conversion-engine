"""
Manages per-lead conversation state and drives the qualification logic.

State machine
-------------
new → outreach_sent → in_conversation → qualified / disqualified

Persistence
-----------
All lead state is stored in SQLite via agent.db.  The in-memory _LEADS dict
has been removed; call db.save_lead(lead) after mutating a Lead object.
"""

import os
import time

import httpx
from dotenv import load_dotenv

from agent.db import Lead, LeadStatus, get_or_create, get_by_phone, link_phone, save_lead
from agent.langfuse_logger import log_span
from agent.retry import http_retry

load_dotenv()

_OR_KEY    = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "openai/gpt-4o-mini")

QUALIFY_AFTER_TURNS = 3
MAX_HISTORY = 40  # cap entries to prevent unbounded growth (~20 exchange turns)

# ── FM-3: system prompt includes offshore objection handling ──────────────────
_SYSTEM_PROMPT = (
    "You are a warm, human B2B sales rep for Tenacious. "
    "Your goal: understand the prospect's pain, build rapport, "
    "and gently move toward booking a 30-minute discovery call. "
    "Keep replies under 80 words. Never be pushy. "
    "If the prospect raises concerns about offshore quality, timezone gaps, "
    "communication barriers, or working with East African engineers, address them "
    "directly and confidently: our engineers are senior, thoroughly vetted, "
    "English-proficient, and overlap 4–6 hours with US East Coast / EU business hours. "
    "Acknowledge the concern before responding, then pivot to a concrete example or offer."
)


@http_retry
def _conv_post(payload: dict, timeout: int = 40) -> httpx.Response:
    return httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {_OR_KEY}"},
        json=payload,
        timeout=timeout,
    )


def _llm_reply(history: list[dict], trace_id: str) -> str:
    messages = [{"role": "system", "content": _SYSTEM_PROMPT}] + [
        {"role": m["role"], "content": m["content"]} for m in history
    ]
    try:
        resp  = _conv_post(
            {
                "model":       _DEV_MODEL,
                "messages":    messages,
                "temperature": float(os.getenv("TEMPERATURE", "0.7")),
                "max_tokens":  200,
            },
            timeout=40,
        )
        reply = resp.json()["choices"][0]["message"]["content"].strip()
        log_span(trace_id, "conversation_reply", messages, reply)
        return reply
    except Exception as exc:
        raise RuntimeError(f"LLM reply failed: {exc}") from exc


def _qualify(lead: Lead, trace_id: str) -> bool:
    """Ask the LLM if this conversation shows buying intent."""
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in lead.history
    )
    try:
        verdict = _conv_post(
            {
                "model":    _DEV_MODEL,
                "messages": [
                    {
                        "role":    "user",
                        "content": (
                            f"Conversation:\n{transcript}\n\n"
                            "Does the prospect show genuine buying intent or interest "
                            "in booking a call? Reply YES or NO only."
                        ),
                    }
                ],
                "temperature": 0.0,
                "max_tokens":  5,
            },
            timeout=20,
        ).json()["choices"][0]["message"]["content"].strip()
        log_span(trace_id, "qualification", transcript, verdict)
        return "YES" in verdict.upper()
    except Exception as exc:
        raise RuntimeError(f"LLM qualification failed: {exc}") from exc


def handle_reply(email: str, text: str, trace_id: str) -> dict:
    """Process an incoming reply. Persists updated lead state to SQLite."""
    lead = get_or_create(email)
    ts   = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    lead.history.append({"role": "user", "content": text, "ts": ts})
    lead.turns  += 1
    lead.status  = "in_conversation"

    agent_reply = _llm_reply(lead.history, trace_id)
    lead.history.append({"role": "assistant", "content": agent_reply, "ts": ts})

    if len(lead.history) > MAX_HISTORY:
        lead.history = lead.history[-MAX_HISTORY:]

    qualified = False
    if lead.turns >= QUALIFY_AFTER_TURNS:
        qualified   = _qualify(lead, trace_id)
        lead.status = "qualified" if qualified else "disqualified"

    save_lead(lead)

    return {
        "lead_id":     lead.lead_id,
        "email":       email,
        "agent_reply": agent_reply,
        "status":      lead.status,
        "qualified":   qualified,
        "turns":       lead.turns,
    }
