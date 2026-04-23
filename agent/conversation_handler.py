"""
Manages per-lead conversation state (in-memory) and drives the qualification logic.

State machine
-------------
new → outreach_sent → in_conversation → qualified / disqualified
"""

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import httpx
from dotenv import load_dotenv

from agent.langfuse_logger import log_span

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-next-80b-a3b-instruct")

LeadStatus = Literal["new", "outreach_sent", "in_conversation", "qualified", "disqualified"]

QUALIFY_AFTER_TURNS = 3   # mark qualified after this many reply turns


@dataclass
class Lead:
    lead_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    email: str = ""
    phone: str = ""                                      # set when AT webhook links phone to lead
    status: LeadStatus = "new"
    history: list[dict] = field(default_factory=list)   # {role, content, ts}
    turns: int = 0
    created_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    profile: dict = field(default_factory=dict)
    booking_url: str = ""
    hubspot_contact_id: str = ""


# Global registry – replace with Redis in production
_LEADS: dict[str, Lead] = {}   # keyed by email


def get_or_create(email: str) -> Lead:
    if email not in _LEADS:
        _LEADS[email] = Lead(email=email)
    return _LEADS[email]


def get_by_phone(phone: str) -> Lead | None:
    """Reverse-lookup a lead by phone number (secondary identifier)."""
    for lead in _LEADS.values():
        if lead.phone == phone:
            return lead
    return None


def link_phone(email: str, phone: str) -> None:
    """Associate an Africa's Talking phone number with an email-keyed lead."""
    lead = _LEADS.get(email)
    if lead and phone:
        lead.phone = phone


def _llm_reply(history: list[dict], trace_id: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You are a warm, human B2B sales rep for Tenacious. "
                "Your goal: understand the prospect's pain, build rapport, "
                "and gently move toward booking a 30-minute discovery call. "
                "Keep replies under 80 words. Never be pushy."
            ),
        }
    ] + [{"role": m["role"], "content": m["content"]} for m in history]

    resp = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {_OR_KEY}"},
        json={
            "model": _DEV_MODEL,
            "messages": messages,
            "temperature": float(os.getenv("TEMPERATURE", "0.7")),
            "max_tokens": 200,
        },
        timeout=40,
    )
    reply = resp.json()["choices"][0]["message"]["content"].strip()
    log_span(trace_id, "conversation_reply", messages, reply)
    return reply


def _qualify(lead: Lead, trace_id: str) -> bool:
    """Ask the LLM if this conversation shows buying intent."""
    transcript = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in lead.history
    )
    verdict = httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {_OR_KEY}"},
        json={
            "model": _DEV_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": (
                        f"Conversation:\n{transcript}\n\n"
                        "Does the prospect show genuine buying intent or interest "
                        "in booking a call? Reply YES or NO only."
                    ),
                }
            ],
            "temperature": 0.0,
            "max_tokens": 5,
        },
        timeout=20,
    ).json()["choices"][0]["message"]["content"].strip()
    log_span(trace_id, "qualification", transcript, verdict)
    return "YES" in verdict.upper()


def handle_reply(email: str, text: str, trace_id: str) -> dict:
    """
    Process an incoming reply.  Returns a dict describing the action taken.
    """
    lead = get_or_create(email)
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    lead.history.append({"role": "user", "content": text, "ts": ts})
    lead.turns += 1
    lead.status = "in_conversation"

    # Generate agent reply
    agent_reply = _llm_reply(lead.history, trace_id)
    lead.history.append({"role": "assistant", "content": agent_reply, "ts": ts})

    qualified = False
    if lead.turns >= QUALIFY_AFTER_TURNS:
        qualified = _qualify(lead, trace_id)
        lead.status = "qualified" if qualified else "in_conversation"

    return {
        "lead_id": lead.lead_id,
        "email": email,
        "agent_reply": agent_reply,
        "status": lead.status,
        "qualified": qualified,
        "turns": lead.turns,
    }
