"""
AI maturity scoring (0–3 scale) with explicit output schema.

Signal-priority scoring ladder:

  Priority 1 (primary)    — Playwright job-post titles: regex matches on known
                            ML/AI job-title keywords and AI tool names.
  Priority 2 (primary)    — GitHub activity: AI/ML-related repository names
                            and commit volume supplied via github_signals dict.
  Priority 3 (secondary)  — AI role ratio: ai_role_count / open_engineering_roles.
  Priority 4 (secondary)  — Patent filings: AI-related patent titles supplied
                            via patent_signals dict.
  Priority 5 (tertiary)   — Domain-name heuristic: ai/ml/neural in root domain.
  Priority 6 (tertiary)   — Conference talks: AI/ML-related presentation titles
                            supplied via conference_signals dict.
  Priority 7 (tiebreaker) — Funding-stage proxy: late-stage funding correlates
                            with AI investment at the population level.
  Priority 8 (floor)      — LLM estimate from _llm_enrich(), capped at score=2.

Output schema
-------------
{
  "score": int,            # 0–3
  "reason": str,           # semicolon-delimited explanation for tracing
  "signals": {
    "job_title_hits":  [str],     # matched title/tool keywords
    "tool_stack_hits": [str],     # matched AI tool names
    "github_repos":    [str],     # AI/ML repo names found in github_signals
    "patents":         [str],     # AI patent titles found in patent_signals
    "conference_talks":[str],     # AI talk titles found in conference_signals
    "ai_role_ratio":   float|None,# ai_role_count / open_engineering_roles
    "domain_match":    bool       # True if domain root triggered heuristic
  }
}
"""

from __future__ import annotations

import re
from typing import Any

# ── Title keyword patterns ────────────────────────────────────────────────────

_HIGH_AI_TITLES = re.compile(
    r"\b(llm|large language model|generative ai|generative model|"
    r"mlops|ml ops|foundation model|ai engineer|ai infrastructure|"
    r"machine learning engineer|ml engineer|nlp engineer|"
    r"computer vision engineer|deep learning|prompt engineer|"
    r"model deployment|inference engineer|embedding|vector search|"
    r"retrieval.augmented|rag engineer)\b",
    re.IGNORECASE,
)

_AI_TOOL_STACK = re.compile(
    r"\b(hugging face|langchain|openai|tensorflow|pytorch|"
    r"vertex ai|sagemaker|aws bedrock|anthropic|cohere|"
    r"pinecone|weaviate|chroma db|chromadb|faiss|llamaindex|"
    r"dspy|vllm|triton inference|ray serve|mlflow|wandb|"
    r"weights.{0,5}biases)\b",
    re.IGNORECASE,
)

_DATA_TITLES = re.compile(
    r"\b(data scientist|data engineer|analytics engineer|"
    r"data platform|data infrastructure|data warehouse|"
    r"bi developer|business intelligence|data analyst)\b",
    re.IGNORECASE,
)

# Patterns that flag a GitHub repo as AI/ML-related
_AI_REPO_PATTERN = re.compile(
    r"\b(llm|gpt|bert|diffusion|transformer|embedding|rag|"
    r"langchain|huggingface|pytorch|tensorflow|ml|ai|nlp|"
    r"generative|inference|fine.tun|vector|semantic|"
    r"neural|deep.learn|model|stable.diffusion)\b",
    re.IGNORECASE,
)

# Patterns that flag a patent title as AI/ML-related
_AI_PATENT_PATTERN = re.compile(
    r"\b(neural network|machine learning|deep learning|"
    r"natural language|computer vision|language model|"
    r"artificial intelligence|generative|inference|"
    r"transformer|embedding|classification model)\b",
    re.IGNORECASE,
)

# Patterns that flag a conference talk title as AI/ML-related
_AI_CONFERENCE_PATTERN = re.compile(
    r"\b(neurips|icml|iclr|acl|emnlp|cvpr|iccv|eccv|"
    r"llm|gpt|diffusion|transformer|fine.tun|rag|"
    r"language model|reinforcement learning|deep learning|"
    r"generative ai|embedding|vector|neural|ai safety)\b",
    re.IGNORECASE,
)

# Funding-stage → probability of significant AI investment
_FUNDING_AI_PROXY: dict[str, float] = {
    "series c":           0.4,
    "series_c":           0.4,
    "series d":           0.6,
    "series_d":           0.6,
    "series e":           0.7,
    "series_e":           0.7,
    "series f":           0.8,
    "series_f":           0.8,
    "growth":             0.5,
    "late stage venture": 0.5,
    "ipo":                0.5,
    "post_ipo_equity":    0.5,
    "post_ipo_debt":      0.4,
}

# Domain roots that strongly suggest an AI-focused product
_AI_DOMAIN_ROOTS = re.compile(
    r"\b(ai|ml|neural|cogni|intelli|synth|neuro|genai|llm|"
    r"deepmind|openai|anthropic|mistral|cohere|hugging)\b",
    re.IGNORECASE,
)


def score_ai_maturity(
    job_signal_value: dict[str, Any],
    cb_signal_value: dict[str, Any],
    domain: str,
    llm_estimate: int = 1,
    github_signals: dict[str, Any] | None = None,
    patent_signals: dict[str, Any] | None = None,
    conference_signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Compute an AI maturity score (0–3) from real enrichment signals.

    Parameters
    ----------
    job_signal_value : dict
        Value dict from Playwright SignalResult.
        Expected keys: open_engineering_roles, ai_role_count, sample_titles.
    cb_signal_value : dict
        Value dict from Crunchbase SignalResult.
        Expected keys: funding_stage.
    domain : str
        Company domain (e.g. "techstartup.io").
    llm_estimate : int
        Raw ai_maturity_score returned by _llm_enrich(). Used as floor only,
        capped at 2 — cannot push score above 2 without real signals.
    github_signals : dict | None
        Optional GitHub enrichment.
        Expected keys: repos (list[str] of repo names),
                       ai_commit_count (int, optional).
    patent_signals : dict | None
        Optional patent enrichment.
        Expected keys: titles (list[str] of patent title strings).
    conference_signals : dict | None
        Optional conference enrichment.
        Expected keys: talks (list[str] of presentation titles or venue names).

    Returns
    -------
    dict with keys: score (int 0-3), reason (str), signals (dict).
    See module docstring for the full output schema.
    """
    score   = 0
    reasons: list[str] = []

    # Collected evidence for the signals dict
    ev_job_titles:  list[str] = []
    ev_tool_hits:   list[str] = []
    ev_gh_repos:    list[str] = []
    ev_patents:     list[str] = []
    ev_conf_talks:  list[str] = []
    ev_ratio:       float | None = None
    ev_domain:      bool = False

    # ── Priority 1: AI title keywords in Playwright sample_titles ────────────
    sample_titles: list[str] = job_signal_value.get("sample_titles") or []
    titles_text = " ".join(str(t) for t in sample_titles)

    high_hits = _HIGH_AI_TITLES.findall(titles_text)
    tool_hits = _AI_TOOL_STACK.findall(titles_text)
    data_hits = _DATA_TITLES.findall(titles_text)

    ev_job_titles = list({h.lower() for h in high_hits})
    ev_tool_hits  = list({h.lower() for h in tool_hits})

    if len(high_hits) >= 3 or len(tool_hits) >= 2:
        score = 3
        reasons.append(
            f"ML/AI job titles (high={len(high_hits)}, tools={len(tool_hits)}): "
            f"{', '.join((ev_job_titles + ev_tool_hits)[:4])}"
        )
    elif len(high_hits) >= 1 or len(tool_hits) >= 1:
        score = max(score, 2)
        reasons.append(
            f"AI/LLM roles detected (high={len(high_hits)}, tools={len(tool_hits)})"
        )
    elif len(data_hits) >= 2:
        score = max(score, 1)
        reasons.append(f"data/analytics roles only (n={len(data_hits)}); not ML-native")

    # ── Priority 2: GitHub AI/ML repository activity ─────────────────────────
    if github_signals:
        repos: list[str] = github_signals.get("repos") or []
        ai_commit_count: int = int(github_signals.get("ai_commit_count") or 0)
        ai_repos = [r for r in repos if _AI_REPO_PATTERN.search(r)]
        ev_gh_repos = ai_repos

        if len(ai_repos) >= 3 or (len(ai_repos) >= 1 and ai_commit_count >= 200):
            if score < 3:
                score = 3
                reasons.append(
                    f"GitHub: {len(ai_repos)} AI/ML repos"
                    + (f", {ai_commit_count} AI commits" if ai_commit_count else "")
                )
        elif len(ai_repos) >= 1 or ai_commit_count >= 50:
            if score < 2:
                score = max(score, 2)
                reasons.append(
                    f"GitHub: {len(ai_repos)} AI/ML repo(s)"
                    + (f", {ai_commit_count} AI commits" if ai_commit_count else "")
                )

    # ── Priority 3: AI role ratio ─────────────────────────────────────────────
    eng_roles = max(int(job_signal_value.get("open_engineering_roles") or 0), 0)
    ai_roles  = max(int(job_signal_value.get("ai_role_count") or 0), 0)

    if eng_roles > 0:
        ratio = ai_roles / eng_roles
        ev_ratio = round(ratio, 3)
        if ratio >= 0.30 and score < 3:
            score = 3
            reasons.append(f"AI role ratio={ratio:.0%} (≥30% of eng roles are AI/ML)")
        elif ratio >= 0.10 and score < 2:
            score = 2
            reasons.append(f"AI role ratio={ratio:.0%} (10–30% of eng roles are AI/ML)")
        elif ratio > 0 and score < 1:
            score = 1
            reasons.append(f"AI role ratio={ratio:.0%} (<10% of eng roles)")
    elif ai_roles >= 2 and score < 2:
        score = max(score, 2)
        reasons.append(f"ai_role_count={ai_roles} with no denominator (all roles may be AI)")

    # ── Priority 4: Patent filings ────────────────────────────────────────────
    if patent_signals:
        titles: list[str] = patent_signals.get("titles") or []
        ai_patents = [t for t in titles if _AI_PATENT_PATTERN.search(t)]
        ev_patents = ai_patents

        if len(ai_patents) >= 3 and score < 3:
            score = 3
            reasons.append(f"patents: {len(ai_patents)} AI/ML patents filed")
        elif len(ai_patents) >= 1 and score < 2:
            score = max(score, 2)
            reasons.append(f"patents: {len(ai_patents)} AI/ML patent(s) detected")

    # ── Priority 5: Domain-name heuristic ────────────────────────────────────
    domain_root = domain.split(".")[0]
    if _AI_DOMAIN_ROOTS.search(domain_root) and score < 2:
        score = max(score, 2)
        ev_domain = True
        reasons.append(f"domain root '{domain_root}' signals AI-native product")
    elif _AI_DOMAIN_ROOTS.search(domain_root):
        ev_domain = True   # record even if it didn't change score

    # ── Priority 6: Conference talks ──────────────────────────────────────────
    if conference_signals:
        talks: list[str] = conference_signals.get("talks") or []
        ai_talks = [t for t in talks if _AI_CONFERENCE_PATTERN.search(t)]
        ev_conf_talks = ai_talks

        if len(ai_talks) >= 2 and score < 2:
            score = max(score, 2)
            reasons.append(f"conference talks: {len(ai_talks)} AI/ML presentations")
        elif len(ai_talks) >= 1 and score < 1:
            score = max(score, 1)
            reasons.append(f"conference talks: {len(ai_talks)} AI/ML talk detected")

    # ── Priority 7: Funding-stage proxy (soft signal only) ───────────────────
    funding_stage = (cb_signal_value.get("funding_stage") or "").lower().strip()
    funding_proxy = _FUNDING_AI_PROXY.get(funding_stage, 0.0)
    if funding_proxy >= 0.5 and score < 1:
        score = max(score, 1)
        reasons.append(
            f"late-stage funding ('{funding_stage}', proxy={funding_proxy:.1f}) "
            "correlates with AI investment"
        )

    # ── Priority 8: LLM estimate as tiebreaker / floor ───────────────────────
    # LLM can raise score to at most 2 — it cannot claim score=3 on its own.
    llm_estimate = max(0, min(3, int(llm_estimate)))

    if score == 0 and llm_estimate > 0:
        score = min(llm_estimate, 2)
        reasons.append(f"LLM estimate={llm_estimate} (no real signals; floor capped at 2)")
    elif score == 1 and llm_estimate >= 2 and not any(
        kw in " ".join(reasons)
        for kw in ("ratio", "title", "tool", "domain", "github", "patent", "conference")
    ):
        score = min(score + 1, llm_estimate, 2)
        reasons.append(f"LLM nudge to {score} (estimate={llm_estimate}, weak real signals)")

    score = max(0, min(3, score))

    reason_str = (
        "; ".join(reasons)
        if reasons
        else "no AI maturity signals detected from job posts, domain, funding stage, or supplemental signals"
    )

    return {
        "score": score,
        "reason": reason_str,
        "signals": {
            "job_title_hits":   ev_job_titles,
            "tool_stack_hits":  ev_tool_hits,
            "github_repos":     ev_gh_repos,
            "patents":          ev_patents,
            "conference_talks": ev_conf_talks,
            "ai_role_ratio":    ev_ratio,
            "domain_match":     ev_domain,
        },
    }
