"""
Competitor gap analysis for Tenacious outreach.

Takes a CompanyProfile and generates a structured competitor brief.

Output schema
-------------
{
  "prospect":          str,
  "domain":            str,
  "industry":          str,          # inferred from job titles / domain
  "segment":           str,          # generic | recently_funded | restructuring_cost | leadership_transition | capability_gap
  "ai_maturity_score": int,          # 0–3
  "competitors": [
    {
      "name": str,
      "positioning": str,
      "gap_analysis": {
        "hiring_trends":      str,   # how the competitor hires vs Tenacious
        "tech_stack_gaps":    [str], # role/stack areas the competitor misses
        "funding_comparison": str,   # funding stage vs this prospect
        "team_growth":        str    # headcount trajectory vs this prospect
      },
      "tenacious_advantages": [str], # concrete advantages over this competitor
      "outreach_angles": {
        "primary":  str,             # strongest hook for this prospect
        "fallback": str              # backup if primary is ignored
      },
      "confidence_score": float      # 0.0–1.0 (how well-matched this comp is)
    }
  ],
  "recommended_angle": str,          # single best outreach hook across all comps
  "top_gap_summary":   str,          # 2-sentence executive gap statement
  "generated_at":      str           # ISO-8601 UTC timestamp
}
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from agent.email_outreach import SEGMENT_LABELS as _SEGMENT_LABELS
from agent.retry import http_retry

load_dotenv()

_OR_KEY    = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "openai/gpt-4o-mini")

# ── Tenacious capability summary injected into every brief ────────────────────
_TENACIOUS_BRIEF = (
    "Tenacious offers elite engineering talent from East Africa (Ethiopia, Kenya). "
    "Specializations: backend, frontend, ML/data engineering, DevOps, mobile. "
    "Placement speed: 2–3 weeks (vs 6–8 week industry average). "
    "Cost: 40–60% lower than equivalent US/EU hiring. "
    "Model: fully managed — Tenacious delivery lead supervises the engineer. "
    "Compliance: GDPR-ready; US business-hours timezone overlap available."
)

# ── Industry inference ────────────────────────────────────────────────────────
_INDUSTRY_SIGNALS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(ml|ai|llm|nlp|computer vision|deep learning)\b", re.I), "AI/ML"),
    (re.compile(r"\b(fintech|payments?|banking|ledger|treasury|lending)\b", re.I), "Fintech"),
    (re.compile(r"\b(health|clinical|medical|pharma|biotech|genomics)\b", re.I), "Healthtech"),
    (re.compile(r"\b(e.?commerce|retail|logistics|supply chain|warehouse)\b", re.I), "Commerce/Logistics"),
    (re.compile(r"\b(devtools?|developer tools?|platform engineering|infra|cloud native)\b", re.I), "DevTools/Infrastructure"),
    (re.compile(r"\b(security|cybersec|soc|siem|devsecops|zero.?trust)\b", re.I), "Cybersecurity"),
    (re.compile(r"\b(saas|b2b software|enterprise software|crm|erp)\b", re.I), "B2B SaaS"),
    (re.compile(r"\b(edtech|lms|learning|education)\b", re.I), "EdTech"),
    (re.compile(r"\b(media|streaming|content|publisher|adtech)\b", re.I), "Media/AdTech"),
]


def _infer_industry(profile: Any) -> str:
    """Return the most likely industry label from job titles and domain."""
    job_val       = profile.job_posts_signal.get("value") or {}
    sample_titles = " ".join(str(t) for t in (job_val.get("sample_titles") or []))
    domain        = profile.domain or ""
    haystack      = f"{sample_titles} {domain}".lower()

    for pattern, label in _INDUSTRY_SIGNALS:
        if pattern.search(haystack):
            return label
    return "B2B Tech"   # safe default


# ── Fallback brief (LLM unavailable) ─────────────────────────────────────────
_FALLBACK_COMPETITORS: list[dict] = [
    {
        "name": "Toptal",
        "positioning": "Premium global freelance network, claims top 3% of talent",
        "gap_analysis": {
            "hiring_trends":      "Vetting queue of 2–4 weeks blocks rapid hiring; no dedicated East Africa bench",
            "tech_stack_gaps":    ["limited ML/AI specialist depth", "no managed delivery model"],
            "funding_comparison": "Bootstrapped/late-stage — no growth-capital urgency to expand Africa coverage",
            "team_growth":        "Flat headcount trajectory for engineers; relies on freelancer churn",
        },
        "tenacious_advantages": [
            "40–60% lower cost at equivalent senior quality",
            "2–3 week placement vs Toptal's 4-week vetting queue",
            "Fully managed — no self-management burden on the prospect's team lead",
        ],
        "outreach_angles": {
            "primary":  "Cut your engineering hiring cost by 50% without sacrificing seniority or speed",
            "fallback": "Tenacious places senior engineers 2× faster than Toptal at half the rate",
        },
        "confidence_score": 0.75,
    },
    {
        "name": "Andela",
        "positioning": "Africa-based engineering talent marketplace",
        "gap_analysis": {
            "hiring_trends":      "Marketplace/batch model — engineers are self-directed; client manages day-to-day",
            "tech_stack_gaps":    ["limited ML/AI specialization depth", "no white-glove managed delivery"],
            "funding_comparison": "Well-funded but pivoting to marketplace; less focused on specialist placements",
            "team_growth":        "Engineer roster growing but quality control delegated to client",
        },
        "tenacious_advantages": [
            "Specialist matching for ML/data roles with individual vetting",
            "Tenacious delivery lead manages the engagement end-to-end",
            "Faster specialist match (2–3 weeks vs Andela's 3–5 week marketplace search)",
        ],
        "outreach_angles": {
            "primary":  "Get a pre-vetted ML engineer with a dedicated Tenacious delivery lead — not a marketplace listing",
            "fallback": "Andela leaves management to you; Tenacious handles it",
        },
        "confidence_score": 0.80,
    },
    {
        "name": "Upwork Enterprise",
        "positioning": "Freelance marketplace with enterprise compliance layer",
        "gap_analysis": {
            "hiring_trends":      "Unmanaged — prospect self-vets, onboards, and manages contractors daily",
            "tech_stack_gaps":    ["high coordination overhead at engineering-team scale", "no specialist bench"],
            "funding_comparison": "Fiverr/Upwork parent is public; no incentive to improve specialist quality",
            "team_growth":        "Relies on commodity contractor pool; high churn rate for senior engineers",
        },
        "tenacious_advantages": [
            "Fully managed model: Tenacious handles vetting, onboarding, and daily stand-ups",
            "Engineering team lead spends 0 hours on contractor management",
            "Dedicated single point of contact vs Upwork's support queue",
        ],
        "outreach_angles": {
            "primary":  "Stop managing contractors — Tenacious's delivery lead handles everything so your team focuses on shipping",
            "fallback": "Enterprise compliance without the coordination overhead of Upwork",
        },
        "confidence_score": 0.70,
    },
]


@http_retry
def _gap_post(payload: dict) -> httpx.Response:
    return httpx.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {_OR_KEY}"},
        json=payload,
        timeout=45,
    )


def generate_competitor_gap_brief(
    profile: Any,
    trace_id: str,
) -> dict:
    """
    Generate a structured competitor gap brief for the prospect.

    Parameters
    ----------
    profile : CompanyProfile
        Fully enriched company profile from enrichment_pipeline.enrich().
    trace_id : str
        Active Langfuse trace ID for span logging.

    Returns
    -------
    dict matching the module-level output schema.
    """
    from agent.langfuse_logger import log_span

    segment_label  = _SEGMENT_LABELS.get(profile.segment, "generic")
    industry_label = _infer_industry(profile)

    job_val        = profile.job_posts_signal.get("value") or {}
    sample_titles  = job_val.get("sample_titles") or []
    ai_role_count  = job_val.get("ai_role_count") or 0
    cb_val         = profile.crunchbase_signal.get("value") or {}
    total_funding  = cb_val.get("total_funding_usd") or 0
    funding_rounds = cb_val.get("funding_rounds") or 0

    ai_labels = {0: "None", 1: "Low", 2: "Medium", 3: "High"}
    ai_label  = ai_labels.get(profile.ai_maturity_score, "Unknown")

    prompt = f"""You are a B2B competitive intelligence analyst for Tenacious.

TENACIOUS OFFERING:
{_TENACIOUS_BRIEF}

PROSPECT:
  Company:              {profile.company_name} ({profile.domain})
  Industry:             {industry_label}
  Segment:              {segment_label}
  AI Maturity:          {profile.ai_maturity_score}/3 ({ai_label})
  Engineering roles:    {profile.open_engineering_roles} open
  AI/ML role count:     {ai_role_count}
  Sample role titles:   {', '.join(str(t) for t in sample_titles[:5]) or 'unknown'}
  Funding stage:        {profile.funding_stage or 'unknown'}
  Total funding:        {'${:,.0f}'.format(total_funding) if total_funding else 'unknown'}
  Funding rounds:       {funding_rounds or 'unknown'}
  Headcount:            ~{profile.headcount or 'unknown'}

TASK:
Identify the 3 most likely staffing/talent competitors Tenacious faces when pitching to {profile.company_name}.
For each competitor, analyse the gap specific to THIS prospect's industry, stack, and growth stage.

Return ONLY a valid JSON object — no markdown fences, no commentary:
{{
  "competitors": [
    {{
      "name": "CompetitorName",
      "positioning": "one sentence on how they position themselves",
      "gap_analysis": {{
        "hiring_trends":      "how they hire vs Tenacious for {profile.company_name}",
        "tech_stack_gaps":    ["gap1", "gap2"],
        "funding_comparison": "their funding stage vs {profile.company_name}'s growth stage",
        "team_growth":        "their headcount / quality trajectory vs this prospect's needs"
      }},
      "tenacious_advantages": ["advantage1", "advantage2", "advantage3"],
      "outreach_angles": {{
        "primary":  "strongest single hook for {profile.company_name}",
        "fallback": "backup hook if primary is ignored"
      }},
      "confidence_score": 0.0
    }}
  ],
  "recommended_angle": "the single most compelling outreach angle for {profile.company_name}",
  "top_gap_summary": "two sentences: the biggest gap Tenacious exploits vs all three competitors for this prospect"
}}"""

    competitors      = _FALLBACK_COMPETITORS
    recommended_angle = (
        f"Cost-effective senior engineering capacity for {profile.company_name} "
        "without the management overhead of a freelance marketplace"
    )
    top_gap_summary = (
        f"No competitor offers {profile.company_name} the combination of "
        "East Africa specialist talent at 40–60% cost reduction with a managed delivery model. "
        f"Tenacious's 2–3 week placement process is the fastest path to qualified "
        f"{segment_label}-stage engineers in the {industry_label} space."
    )

    if _OR_KEY:
        try:
            resp = _gap_post({
                "model":           _DEV_MODEL,
                "messages":        [{"role": "user", "content": prompt}],
                "temperature":     0.3,
                "max_tokens":      900,
                "response_format": {"type": "json_object"},
            })
            raw_text = resp.json()["choices"][0]["message"]["content"].strip()
            # Strip any accidental markdown fences
            raw_text = raw_text.lstrip("```json").lstrip("```").rstrip("```").strip()
            data = json.loads(raw_text)

            if "competitors" in data and len(data["competitors"]) >= 1:
                competitors       = data["competitors"]
                recommended_angle = data.get("recommended_angle", recommended_angle)
                top_gap_summary   = data.get("top_gap_summary", top_gap_summary)
        except Exception as exc:
            log_span(
                trace_id, "competitor_gap_llm_error",
                {"domain": profile.domain, "error": str(exc)},
                {"fallback": True},
            )

    brief: dict = {
        "prospect":          profile.company_name,
        "domain":            profile.domain,
        "industry":          industry_label,
        "segment":           segment_label,
        "ai_maturity_score": profile.ai_maturity_score,
        "competitors":       competitors,
        "recommended_angle": recommended_angle,
        "top_gap_summary":   top_gap_summary,
        "generated_at":      datetime.now(timezone.utc).isoformat(),
    }

    # Attach ai_maturity_reason if the enrichment pipeline stored it
    ai_reason = (getattr(profile, "raw", None) or {}).get("ai_maturity_reason")
    if ai_reason:
        brief["ai_maturity_reason"] = ai_reason

    log_span(trace_id, "competitor_gap_brief", {"prospect": profile.company_name}, brief)
    return brief
