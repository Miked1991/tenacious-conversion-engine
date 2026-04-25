"""
Deterministic AI maturity scoring (0–3 scale).

Replaces pure LLM delegation with a signal-priority scoring ladder:

  Priority 1 (primary)  — Playwright job-post titles: regex matches on known
                          ML/AI job-title keywords and AI tool names.
  Priority 2 (secondary)— AI role ratio: ai_role_count / open_engineering_roles.
  Priority 3 (tertiary) — Domain-name heuristic: ai/ml/neural in root domain.
  Priority 4 (tiebreaker)— Funding-stage proxy: late-stage funding correlates
                          with AI investment at the population level.
  Priority 5 (floor)    — LLM estimate from _llm_enrich(), capped at score=1
                          when no real signals fired, or used as a +1 nudge when
                          real signals are weak.

Output: (score: int 0–3, reason: str) — reason is machine-readable and
stored in CompanyProfile for downstream prompt context and traceability.
"""

import re

# ── Title keyword patterns ────────────────────────────────────────────────────

# High-confidence AI indicators: LLM/foundation-model/MLOps titles
_HIGH_AI_TITLES = re.compile(
    r"\b(llm|large language model|generative ai|generative model|"
    r"mlops|ml ops|foundation model|ai engineer|ai infrastructure|"
    r"machine learning engineer|ml engineer|nlp engineer|"
    r"computer vision engineer|deep learning|prompt engineer|"
    r"model deployment|inference engineer|embedding|vector search|"
    r"retrieval.augmented|rag engineer)\b",
    re.IGNORECASE,
)

# Medium-confidence: AI tools mentioned in job posts
_AI_TOOL_STACK = re.compile(
    r"\b(hugging face|langchain|openai|tensorflow|pytorch|"
    r"vertex ai|sagemaker|aws bedrock|anthropic|cohere|"
    r"pinecone|weaviate|chroma db|chromadb|faiss|llamaindex|"
    r"dspy|vllm|triton inference|ray serve|mlflow|wandb|"
    r"weights.{0,5}biases)\b",
    re.IGNORECASE,
)

# Low-confidence: data roles (analytics, BI) — not ML/AI but related
_DATA_TITLES = re.compile(
    r"\b(data scientist|data engineer|analytics engineer|"
    r"data platform|data infrastructure|data warehouse|"
    r"bi developer|business intelligence|data analyst)\b",
    re.IGNORECASE,
)

# Funding-stage → probability of significant AI investment
_FUNDING_AI_PROXY: dict[str, float] = {
    "series c": 0.4,
    "series_c": 0.4,
    "series d": 0.6,
    "series_d": 0.6,
    "series e": 0.7,
    "series_e": 0.7,
    "series f": 0.8,
    "series_f": 0.8,
    "growth": 0.5,
    "late stage venture": 0.5,
    "ipo": 0.5,
    "post_ipo_equity": 0.5,
    "post_ipo_debt": 0.4,
}

# Domain roots that strongly suggest AI-focused product
_AI_DOMAIN_ROOTS = re.compile(
    r"\b(ai|ml|neural|cogni|intelli|synth|neuro|genai|llm|"
    r"deepmind|openai|anthropic|mistral|cohere|hugging)\b",
    re.IGNORECASE,
)


def score_ai_maturity(
    job_signal_value: dict,
    cb_signal_value: dict,
    domain: str,
    llm_estimate: int = 1,
) -> tuple[int, str]:
    """
    Compute an AI maturity score (0–3) from real enrichment signals.

    Parameters
    ----------
    job_signal_value : dict
        Value dict from Playwright SignalResult (keys: open_engineering_roles,
        ai_role_count, sample_titles).
    cb_signal_value : dict
        Value dict from Crunchbase SignalResult (keys: funding_stage, etc.).
    domain : str
        Company domain (e.g. "techstartup.io") — used for domain heuristic.
    llm_estimate : int
        Raw ai_maturity_score returned by _llm_enrich() — used as tiebreaker.

    Returns
    -------
    (score, reason) where score in [0, 3] and reason is a semicolon-delimited
    explanation string for tracing and prompt injection.
    """
    score = 0
    reasons: list[str] = []

    # ── Priority 1: AI title keywords in Playwright sample_titles ────────────
    sample_titles: list[str] = job_signal_value.get("sample_titles") or []
    titles_text = " ".join(str(t) for t in sample_titles)

    high_hits = _HIGH_AI_TITLES.findall(titles_text)
    tool_hits = _AI_TOOL_STACK.findall(titles_text)
    data_hits = _DATA_TITLES.findall(titles_text)

    if len(high_hits) >= 3 or len(tool_hits) >= 2:
        score = 3
        reasons.append(
            f"ML/AI job titles (high={len(high_hits)}, tools={len(tool_hits)}): "
            f"{', '.join(set(h.lower() for h in (high_hits + tool_hits)[:4]))}"
        )
    elif len(high_hits) >= 1 or len(tool_hits) >= 1:
        score = max(score, 2)
        reasons.append(
            f"AI/LLM roles detected (high={len(high_hits)}, tools={len(tool_hits)})"
        )
    elif len(data_hits) >= 2:
        score = max(score, 1)
        reasons.append(f"data/analytics roles only (n={len(data_hits)}); not ML-native")

    # ── Priority 2: AI role ratio ─────────────────────────────────────────────
    eng_roles = max(int(job_signal_value.get("open_engineering_roles") or 0), 0)
    ai_roles  = max(int(job_signal_value.get("ai_role_count") or 0), 0)

    if eng_roles > 0:
        ratio = ai_roles / eng_roles
        if ratio >= 0.30 and score < 3:
            score = 3
            reasons.append(f"AI role ratio={ratio:.0%} (>=30% of eng roles are AI/ML)")
        elif ratio >= 0.10 and score < 2:
            score = 2
            reasons.append(f"AI role ratio={ratio:.0%} (10–30% of eng roles are AI/ML)")
        elif ratio > 0 and score < 1:
            score = 1
            reasons.append(f"AI role ratio={ratio:.0%} (<10% of eng roles)")
    elif ai_roles >= 2 and score < 2:
        score = max(score, 2)
        reasons.append(f"ai_role_count={ai_roles} with no denominator (all roles may be AI)")

    # ── Priority 3: Domain-name heuristic ────────────────────────────────────
    domain_root = domain.split(".")[0]
    if _AI_DOMAIN_ROOTS.search(domain_root) and score < 2:
        score = max(score, 2)
        reasons.append(f"domain root '{domain_root}' signals AI-native product")

    # ── Priority 4: Funding-stage proxy (soft signal only) ───────────────────
    funding_stage = (cb_signal_value.get("funding_stage") or "").lower().strip()
    funding_proxy = _FUNDING_AI_PROXY.get(funding_stage, 0.0)
    if funding_proxy >= 0.5 and score < 2:
        score = max(score, 1)
        reasons.append(
            f"late-stage funding ('{funding_stage}', proxy={funding_proxy:.1f}) "
            "correlates with AI investment"
        )

    # ── Priority 5: LLM estimate as tiebreaker / floor ───────────────────────
    llm_estimate = max(0, min(3, int(llm_estimate)))

    if score == 0 and llm_estimate > 0:
        # No real signals fired — LLM can provide a floor of at most 1
        score = min(llm_estimate, 1)
        reasons.append(f"LLM estimate={llm_estimate} (no real signals; floor capped at 1)")
    elif score == 1 and llm_estimate >= 2 and not any(
        kw in " ".join(reasons) for kw in ("ratio", "title", "tool", "domain")
    ):
        # Weak real signals + stronger LLM estimate → nudge up by 1
        score = min(score + 1, llm_estimate)
        reasons.append(f"LLM nudge to {score} (estimate={llm_estimate}, weak real signals)")

    # Clamp to valid range
    score = max(0, min(3, score))

    reason_str = (
        "; ".join(reasons)
        if reasons
        else "no AI maturity signals detected from job posts, domain, or funding stage"
    )

    return score, reason_str
