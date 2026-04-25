"""
Competitor gap brief generation for Tenacious outreach.

Takes a CompanyProfile and generates a structured competitor analysis:
  - 3 competitors relevant to the prospect's industry and engineering stack
  - Gap: what each competitor lacks that Tenacious can fill
  - Tenacious advantage per competitor
  - Single recommended outreach angle
  - Two-sentence top gap summary

Output schema matches eval/competitor_gap_brief.json and is used both in
email composition prompts and saved to eval/crunchbase-result/ for review.

Wired into _run_full_pipeline() in main.py; result stored on lead profile.
"""

import json
import os
import time

import httpx
from dotenv import load_dotenv

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-next-80b-a3b-instruct")

_SEGMENT_LABELS = {
    0: "generic",
    1: "recently_funded",
    2: "post_layoff",
    3: "hypergrowth",
}

# ── Tenacious capability summary injected into every brief ────────────────────
_TENACIOUS_BRIEF = (
    "Tenacious offers elite engineering talent from East Africa (Ethiopia, Kenya). "
    "Specializations: backend, frontend, ML/data engineering, DevOps, mobile. "
    "Placement speed: 2–3 weeks (vs 6–8 week industry average). "
    "Cost: 40–60% lower than equivalent US/EU hiring. "
    "Model: fully managed — Tenacious delivery lead supervises the engineer. "
    "Compliance: GDPR-ready; US business-hours timezone overlap available."
)

# ── Fallback brief when LLM is unavailable ────────────────────────────────────
_FALLBACK_COMPETITORS = [
    {
        "name": "Toptal",
        "positioning": "Premium global freelance network, claims top 3% of talent",
        "gap": "High cost ($150–200/hr) and 2–4 week vetting delay; no East Africa presence",
        "tenacious_advantage": (
            "40–60% lower cost with equivalent senior quality "
            "and 2–3 week placement vs Toptal's 4-week vetting"
        ),
    },
    {
        "name": "Andela",
        "positioning": "Africa-based engineering talent marketplace",
        "gap": (
            "Batch / marketplace model — prospect must self-manage engineers; "
            "limited ML/AI specialization depth"
        ),
        "tenacious_advantage": (
            "Specialist matching for ML/data roles with individual vetting "
            "and a Tenacious delivery lead who manages the engagement"
        ),
    },
    {
        "name": "Upwork Enterprise",
        "positioning": "Freelance marketplace with enterprise compliance layer",
        "gap": (
            "Unmanaged — prospect vets, manages, and retains contractors themselves; "
            "high coordination overhead at engineering-team scale"
        ),
        "tenacious_advantage": (
            "Fully managed model: Tenacious handles vetting, onboarding, and daily "
            "management so the engineering team lead can focus on product"
        ),
    },
]


def generate_competitor_gap_brief(
    profile,      # agent.enrichment_pipeline.CompanyProfile
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
    dict with keys: prospect, domain, segment, ai_maturity_score,
    competitors (list[3]), recommended_angle, top_gap_summary,
    generated_at, ai_maturity_reason (if available).
    """
    from agent.langfuse_logger import log_span

    segment_label = _SEGMENT_LABELS.get(profile.segment, "generic")

    # Build context from enrichment signals for richer LLM prompt
    job_val        = profile.job_posts_signal.get("value") or {}
    sample_titles  = job_val.get("sample_titles") or []
    ai_role_count  = job_val.get("ai_role_count") or 0
    cb_val         = profile.crunchbase_signal.get("value") or {}
    total_funding  = cb_val.get("total_funding_usd") or 0
    funding_rounds = cb_val.get("funding_rounds") or 0

    ai_maturity_labels = {0: "None", 1: "Low", 2: "Medium", 3: "High"}
    ai_label = ai_maturity_labels.get(profile.ai_maturity_score, "Unknown")

    prompt = f"""You are a B2B competitive intelligence analyst for Tenacious.

TENACIOUS OFFERING:
{_TENACIOUS_BRIEF}

PROSPECT:
  Company: {profile.company_name} ({profile.domain})
  Segment: {segment_label}
  AI Maturity: {profile.ai_maturity_score}/3 ({ai_label})
  Engineering roles open: {profile.open_engineering_roles}
  AI/ML role count: {ai_role_count}
  Sample role titles: {', '.join(str(t) for t in sample_titles[:5]) or 'unknown'}
  Funding stage: {profile.funding_stage or 'unknown'}
  Total funding: {'${:,.0f}'.format(total_funding) if total_funding else 'unknown'}
  Funding rounds: {funding_rounds or 'unknown'}
  Headcount: ~{profile.headcount or 'unknown'}

TASK:
Identify the 3 most likely competitors Tenacious faces when pitching to {profile.company_name}.
Consider the prospect's industry, tech stack (inferred from role titles), and growth stage.
For each competitor, identify a specific gap relevant to THIS prospect's situation.

Return ONLY a valid JSON object — no markdown, no explanation, no trailing text:
{{
  "competitors": [
    {{
      "name": "CompetitorName",
      "positioning": "one sentence on how they position themselves",
      "gap": "specific gap relevant to {profile.company_name} given their stack and growth stage",
      "tenacious_advantage": "concrete Tenacious advantage over this competitor for this prospect"
    }},
    {{
      "name": "CompetitorName2",
      "positioning": "...",
      "gap": "...",
      "tenacious_advantage": "..."
    }},
    {{
      "name": "CompetitorName3",
      "positioning": "...",
      "gap": "...",
      "tenacious_advantage": "..."
    }}
  ],
  "recommended_angle": "single most compelling outreach angle for {profile.company_name} given their situation",
  "top_gap_summary": "two sentences: the biggest competitive gap Tenacious can exploit for this specific prospect"
}}"""

    competitors = _FALLBACK_COMPETITORS
    recommended_angle = (
        f"Cost-effective senior engineering capacity for {profile.company_name} "
        "without the management overhead of a freelance marketplace"
    )
    top_gap_summary = (
        f"No competitor offers {profile.company_name} the combination of "
        "East Africa specialist talent at 40–60% cost reduction with a managed delivery model. "
        f"Tenacious's 2–3 week placement process is the fastest path to qualified {segment_label}-stage engineers."
    )

    try:
        resp = httpx.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {_OR_KEY}"},
            json={
                "model": _DEV_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 700,
                "response_format": {"type": "json_object"},
            },
            timeout=45,
        )
        text = resp.json()["choices"][0]["message"]["content"].strip()
        text = text.lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(text)
        if "competitors" in data and len(data["competitors"]) == 3:
            competitors      = data["competitors"]
            recommended_angle = data.get("recommended_angle", recommended_angle)
            top_gap_summary   = data.get("top_gap_summary", top_gap_summary)
    except Exception as exc:
        log_span(
            trace_id, "competitor_gap_llm_error",
            {"domain": profile.domain, "error": str(exc)},
            {"fallback": True},
        )

    brief = {
        "prospect":          profile.company_name,
        "domain":            profile.domain,
        "segment":           segment_label,
        "ai_maturity_score": profile.ai_maturity_score,
        "competitors":       competitors,
        "recommended_angle": recommended_angle,
        "top_gap_summary":   top_gap_summary,
        "generated_at":      time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    log_span(trace_id, "competitor_gap_brief", {"prospect": profile.company_name}, brief)
    return brief
