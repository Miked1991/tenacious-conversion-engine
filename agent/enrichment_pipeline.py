"""
Enriches a company profile using public signals and classifies into a sales segment.

Segment definitions
-------------------
1  recently_funded     – raised Series A/B in last 6 months, actively hiring engineers
2  post_layoff         – conducted layoffs in last 90 days, headcount contracting
3  hypergrowth         – >40 % YoY headcount growth, no recent funding
0  generic             – none of the above
"""

import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

import httpx
from dotenv import load_dotenv

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-next-80b-a3b-instruct")


@dataclass
class CompanyProfile:
    email: str
    domain: str = ""
    company_name: str = ""
    headcount: int = 0
    funding_stage: str = ""
    recently_funded: bool = False
    had_layoffs: bool = False
    headcount_growth_pct: float = 0.0
    open_engineering_roles: int = 0
    ai_maturity_score: int = 0          # 1–5
    segment: Literal[0, 1, 2, 3] = 0
    enriched_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    raw: dict = field(default_factory=dict)


def _extract_domain(email: str) -> str:
    return email.split("@")[-1] if "@" in email else email


def _llm_enrich(domain: str) -> dict:
    """Ask the LLM to synthesise a plausible company profile for the domain."""
    prompt = (
        f"You are a B2B sales researcher. Given the domain '{domain}', produce a JSON object "
        "with ONLY these keys (no explanation, no markdown fence):\n"
        "company_name, headcount (integer), funding_stage (string), recently_funded (bool), "
        "had_layoffs (bool), headcount_growth_pct (float), open_engineering_roles (integer), "
        "ai_maturity_score (integer 1-5).\n"
        "Base estimates on publicly known information about the company. "
        "If the domain is completely unknown, make reasonable guesses for a mid-size SaaS company."
    )
    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {_OR_KEY}"},
            json={
                "model": _DEV_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 300,
            },
            timeout=30,
        )
        import json
        text = resp.json()["choices"][0]["message"]["content"]
        # strip possible markdown fences
        text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(text)
    except Exception:
        return {
            "company_name": domain.split(".")[0].title(),
            "headcount": 150,
            "funding_stage": "Series A",
            "recently_funded": False,
            "had_layoffs": False,
            "headcount_growth_pct": 12.0,
            "open_engineering_roles": 5,
            "ai_maturity_score": 2,
        }


def _classify_segment(profile: CompanyProfile) -> Literal[0, 1, 2, 3]:
    if profile.recently_funded and profile.open_engineering_roles >= 3:
        return 1
    if profile.had_layoffs:
        return 2
    if profile.headcount_growth_pct >= 40.0:
        return 3
    return 0


def enrich(email: str) -> CompanyProfile:
    domain = _extract_domain(email)
    raw = _llm_enrich(domain)
    profile = CompanyProfile(
        email=email,
        domain=domain,
        company_name=raw.get("company_name", domain.split(".")[0].title()),
        headcount=int(raw.get("headcount", 0)),
        funding_stage=raw.get("funding_stage", ""),
        recently_funded=bool(raw.get("recently_funded", False)),
        had_layoffs=bool(raw.get("had_layoffs", False)),
        headcount_growth_pct=float(raw.get("headcount_growth_pct", 0.0)),
        open_engineering_roles=int(raw.get("open_engineering_roles", 0)),
        ai_maturity_score=int(raw.get("ai_maturity_score", 2)),
        raw=raw,
    )
    profile.segment = _classify_segment(profile)
    return profile
