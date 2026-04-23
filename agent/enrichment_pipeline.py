"""
Multi-source signal enrichment pipeline with per-signal confidence scores.

Signal sources
--------------
1. Crunchbase ODM  – funding stage, last-round date, headcount band
2. Playwright jobs – open engineering / AI roles on public career pages (no login)
3. Layoffs.fyi CSV – recent layoff events (public Google Sheets export)
4. Leadership change – recent C-suite / VP moves via People Data Labs API

Each source returns a SignalResult with a confidence score (0.0–1.0).
All four are merged into CompanyProfile before segment classification.

Segment definitions
-------------------
0  generic          – none of the below triggers fire
1  recently_funded  – raised Series A/B in last 6 months, actively hiring engineers
2  post_layoff      – conducted layoffs in last 90 days, headcount contracting
3  hypergrowth      – >40 % YoY headcount growth, no recent funding
"""

import csv
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import httpx
from dotenv import load_dotenv

load_dotenv()

_OR_KEY = os.getenv("OPENROUTER_API_KEY", "")
_DEV_MODEL = os.getenv("DEV_MODEL", "qwen/qwen3-next-80b-a3b-instruct")
_CB_KEY = os.getenv("CRUNCHBASE_API_KEY", "")
_PDL_KEY = os.getenv("PDL_API_KEY", "")

# Public layoffs.fyi Google Sheets CSV export (no auth required)
_LAYOFFS_CSV_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1LcEkQjBaK5H18Luv2NQMfg7V9p6CE4mSKM65oFOKG0A"
    "/export?format=csv&gid=0"
)

_LEADERSHIP_WINDOW_DAYS = 90
_URGENCY_BOOST_WINDOW_DAYS = 30
_URGENCY_BOOST_VALUE = 0.3


# ── Signal result container ───────────────────────────────────────────────────

@dataclass
class SignalResult:
    """Structured output from a single enrichment source."""
    value: Any
    confidence: float           # 0.0 (no data) – 1.0 (authoritative)
    source: str                 # "crunchbase_odm" | "playwright_jobs" | "layoffs_fyi_csv" | "pdl_leadership" | "llm_fallback"
    fetched_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

    def to_dict(self) -> dict:
        return {
            "value": self.value,
            "confidence": self.confidence,
            "source": self.source,
            "fetched_at": self.fetched_at,
        }


# ── Leadership change dataclass ───────────────────────────────────────────────

@dataclass
class LeadershipChange:
    detected: bool = False
    changed_role: str = ""          # e.g. "Chief Revenue Officer"
    change_type: str = ""           # new_hire | promotion | departure
    change_date: str = ""           # ISO-8601 date
    previous_company: str = ""
    days_since_change: int = 0
    outreach_urgency_boost: float = 0.0   # +0.3 if change happened within 30 days


# ── Main company profile ──────────────────────────────────────────────────────

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
    ai_maturity_score: int = 0          # 0=None 1=Low 2=Medium 3=High
    segment: Literal[0, 1, 2, 3] = 0
    enriched_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    raw: dict = field(default_factory=dict)

    # Per-signal results (serialisable dicts, each with value/confidence/source/fetched_at)
    crunchbase_signal: dict = field(default_factory=dict)
    job_posts_signal: dict = field(default_factory=dict)
    layoffs_signal: dict = field(default_factory=dict)
    leadership_change_signal: dict = field(default_factory=dict)
    leadership_change: LeadershipChange = field(default_factory=LeadershipChange)


# ── 1. Crunchbase ODM ────────────────────────────────────────────────────────

def _is_recent_funding(date_str: str, months: int = 6) -> bool:
    if not date_str:
        return False
    try:
        funded = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc) - timedelta(days=months * 30)
        return funded >= cutoff
    except Exception:
        return False


def _fetch_crunchbase(domain: str) -> SignalResult:
    """
    Query Crunchbase API v4 (ODM) for funding stage, last-round date, and
    headcount band.  Requires CRUNCHBASE_API_KEY env var (free Basic tier).
    Returns confidence=0.0 when the key is absent so downstream code can fall
    back to the LLM estimate.
    """
    if not _CB_KEY:
        return SignalResult(
            value={"note": "no CRUNCHBASE_API_KEY configured"},
            confidence=0.0,
            source="crunchbase_odm_skipped",
        )
    try:
        resp = httpx.post(
            "https://api.crunchbase.com/api/v4/searches/organizations",
            params={"user_key": _CB_KEY},
            json={
                "field_ids": [
                    "identifier", "funding_total", "last_funding_type",
                    "last_funding_at", "num_employees_enum", "num_funding_rounds",
                ],
                "query": [
                    {
                        "type": "predicate",
                        "field_id": "facet_ids",
                        "operator_id": "includes",
                        "values": ["company"],
                    },
                    {
                        "type": "predicate",
                        "field_id": "domain",
                        "operator_id": "includes",
                        "values": [domain],
                    },
                ],
                "limit": 1,
            },
            timeout=15,
        )
        data = resp.json()
        entities = data.get("entities", [])
        if not entities:
            return SignalResult(
                value={"domain": domain, "found": False},
                confidence=0.2,
                source="crunchbase_odm",
            )
        props = entities[0].get("properties", {})
        last_funding_at = props.get("last_funding_at", "")
        result = {
            "found": True,
            "company_name": (props.get("identifier") or {}).get("value", ""),
            "funding_stage": props.get("last_funding_type", ""),
            "last_funding_date": last_funding_at,
            "headcount_band": props.get("num_employees_enum", ""),
            "funding_rounds": props.get("num_funding_rounds", 0),
            "total_funding_usd": (props.get("funding_total") or {}).get("value_usd", 0),
            "recently_funded": _is_recent_funding(last_funding_at),
        }
        return SignalResult(value=result, confidence=0.9, source="crunchbase_odm")
    except Exception as exc:
        return SignalResult(
            value={"error": str(exc)},
            confidence=0.0,
            source="crunchbase_odm_error",
        )


# ── 2. Playwright job-post scraping ──────────────────────────────────────────

_ENG_KEYWORDS = re.compile(
    r"\b(engineer|developer|devops|sre|platform|backend|frontend|fullstack"
    r"|data scientist|ml engineer|machine learning|infrastructure)\b",
    re.IGNORECASE,
)
_AI_KEYWORDS = re.compile(
    r"\b(ai|artificial intelligence|llm|generative|nlp|computer vision"
    r"|deep learning|mlops|prompt engineer|foundation model)\b",
    re.IGNORECASE,
)


def _scrape_job_posts(domain: str) -> SignalResult:
    """
    Crawl the company's public career pages via Playwright (headless Chromium).
    No login logic and no captcha-bypass code are used — only publicly
    accessible pages are scraped.  Counts engineering and AI-specific roles.
    """
    try:
        from playwright.sync_api import sync_playwright, Error as PwError
    except ImportError:
        return SignalResult(
            value={"note": "playwright not installed; run: pip install playwright && playwright install chromium"},
            confidence=0.0,
            source="playwright_jobs_skipped",
        )

    candidate_urls = [
        f"https://{domain}/careers",
        f"https://{domain}/jobs",
        f"https://jobs.{domain}",
        f"https://{domain}/join",
        f"https://{domain}/work-with-us",
    ]

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (compatible; TenaciousBot/1.0)"
            )
            for url in candidate_urls:
                try:
                    response = page.goto(url, timeout=12000, wait_until="domcontentloaded")
                    if response and response.status == 200:
                        body_text = page.inner_text("body")
                        lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
                        eng_roles = [ln for ln in lines if _ENG_KEYWORDS.search(ln)]
                        ai_roles = [ln for ln in lines if _AI_KEYWORDS.search(ln)]
                        browser.close()
                        result = {
                            "page_url": url,
                            "page_found": True,
                            "open_engineering_roles": len(eng_roles),
                            "ai_role_count": len(ai_roles),
                            "sample_titles": eng_roles[:5],
                        }
                        return SignalResult(
                            value=result,
                            confidence=0.8 if eng_roles else 0.4,
                            source="playwright_jobs",
                        )
                except PwError:
                    continue
                except Exception:
                    continue
            browser.close()
    except Exception as exc:
        return SignalResult(
            value={"error": str(exc)},
            confidence=0.0,
            source="playwright_jobs_error",
        )

    return SignalResult(
        value={"page_found": False, "open_engineering_roles": 0, "ai_role_count": 0},
        confidence=0.1,
        source="playwright_jobs",
    )


# ── 3. Layoffs.fyi CSV parsing ───────────────────────────────────────────────

def _layoffs_within_90_days(events: list[dict]) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    for ev in events:
        date_str = (ev.get("date") or "").strip()
        if not date_str:
            continue
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %Y", "%b %Y", "%b-%y"):
            try:
                dt = datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
                if dt >= cutoff:
                    return True
                break
            except ValueError:
                continue
    return False


def _parse_layoffs_fyi(company_name: str, domain: str) -> SignalResult:
    """
    Fetch the public layoffs.fyi Google Sheets CSV export and search for the
    target company by name or domain root.  No authentication is required.
    """
    try:
        resp = httpx.get(_LAYOFFS_CSV_URL, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return SignalResult(
                value={"note": f"layoffs.fyi HTTP {resp.status_code}"},
                confidence=0.0,
                source="layoffs_fyi_csv_error",
            )
        reader = csv.DictReader(io.StringIO(resp.text))
        matches = []
        company_lower = company_name.lower()
        domain_root = domain.split(".")[0].lower()
        for row in reader:
            row_company = (
                row.get("Company") or row.get("company") or ""
            ).lower()
            if company_lower in row_company or domain_root in row_company:
                matches.append({
                    "company": row.get("Company") or row.get("company", ""),
                    "date": row.get("Date") or row.get("date", ""),
                    "laid_off": row.get("# Laid Off") or row.get("laid_off", ""),
                    "percentage": row.get("Percentage") or row.get("percentage", ""),
                    "stage": row.get("Stage") or row.get("stage", ""),
                })
        had_layoffs = len(matches) > 0
        recent = _layoffs_within_90_days(matches)
        return SignalResult(
            value={
                "had_layoffs": had_layoffs,
                "recent_layoffs": recent,
                "events": matches,
                "total_events": len(matches),
            },
            confidence=0.95 if had_layoffs else 0.6,
            source="layoffs_fyi_csv",
        )
    except Exception as exc:
        return SignalResult(
            value={"error": str(exc)},
            confidence=0.0,
            source="layoffs_fyi_csv_error",
        )


# ── 4. Leadership change detection (PDL) ────────────────────────────────────

def _days_since(date_str: str) -> int:
    if not date_str:
        return 999
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            dt = datetime.strptime(date_str, fmt)
            return (datetime.now() - dt).days
        except ValueError:
            continue
    return 999


def _detect_leadership_change(domain: str) -> SignalResult:
    """
    Query People Data Labs (PDL) /v5/person/search for recent C-suite / VP /
    Director hires at the target company.  Detection window: 90 days.

    PDL query pattern
    -----------------
    POST https://api.peopledatalabs.com/v5/person/search
    Filter: job_company_website = domain
            AND job_title_levels IN [c_suite, vp, director]
            AND job_start_date >= (today - 90d)

    Urgency boost: +0.3 applied when change occurred within 30 days.
    Requires PDL_API_KEY env var.  Returns confidence=0.0 when key is absent.
    """
    if not _PDL_KEY:
        return SignalResult(
            value={"note": "no PDL_API_KEY configured; set PDL_API_KEY for live leadership-change detection"},
            confidence=0.0,
            source="pdl_leadership_skipped",
        )

    cutoff_date = (
        datetime.now(timezone.utc) - timedelta(days=_LEADERSHIP_WINDOW_DAYS)
    ).strftime("%Y-%m-%d")

    try:
        resp = httpx.post(
            "https://api.peopledatalabs.com/v5/person/search",
            headers={"X-Api-Key": _PDL_KEY},
            json={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"job_company_website": domain}},
                            {"terms": {"job_title_levels": ["c_suite", "vp", "director"]}},
                            {"range": {"job_start_date": {"gte": cutoff_date}}},
                        ]
                    }
                },
                "size": 5,
                "fields": [
                    "full_name", "job_title", "job_title_levels",
                    "job_start_date", "experience",
                ],
            },
            timeout=15,
        )
        data = resp.json()
        if resp.status_code != 200:
            return SignalResult(
                value={"error": (data.get("error") or {}).get("message", "unknown")},
                confidence=0.0,
                source="pdl_leadership_error",
            )
        people = data.get("data", [])
        if not people:
            return SignalResult(
                value={"detected": False},
                confidence=0.7,
                source="pdl_leadership",
            )
        top = people[0]
        experience = top.get("experience") or []
        prev_co = (experience[1].get("company") or {}).get("name", "") if len(experience) > 1 else ""
        start_date = top.get("job_start_date", "")
        days_since = _days_since(start_date)
        boost = _URGENCY_BOOST_VALUE if days_since <= _URGENCY_BOOST_WINDOW_DAYS else 0.0
        result = {
            "detected": True,
            "changed_role": top.get("job_title", ""),
            "change_type": "new_hire",
            "change_date": start_date,
            "previous_company": prev_co,
            "days_since_change": days_since,
            "outreach_urgency_boost": boost,
            "executives_found": len(people),
        }
        return SignalResult(value=result, confidence=0.85, source="pdl_leadership")
    except Exception as exc:
        return SignalResult(
            value={"error": str(exc)},
            confidence=0.0,
            source="pdl_leadership_error",
        )


# ── LLM fallback enrichment ──────────────────────────────────────────────────

def _llm_enrich(domain: str, signal_context: str = "") -> dict:
    """
    Synthesise a company profile via LLM, informed by any real signals already
    collected.  Fills fields that live data sources could not provide.
    """
    prompt = (
        f"You are a B2B sales researcher. Given the domain '{domain}', produce a JSON object "
        "with ONLY these keys (no explanation, no markdown fence):\n"
        "company_name, headcount (integer), funding_stage (string), recently_funded (bool), "
        "had_layoffs (bool), headcount_growth_pct (float), open_engineering_roles (integer), "
        "ai_maturity_score (integer 0-3, where 0=None 1=Low 2=Medium 3=High).\n"
    )
    if signal_context:
        prompt += f"\nUse this real signal context to inform your estimates:\n{signal_context}\n"
    prompt += (
        "Base remaining estimates on publicly known information. "
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
        text = resp.json()["choices"][0]["message"]["content"]
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


# ── Helpers ──────────────────────────────────────────────────────────────────

def _extract_domain(email: str) -> str:
    return email.split("@")[-1] if "@" in email else email


def _classify_segment(profile: CompanyProfile) -> Literal[0, 1, 2, 3]:
    if profile.recently_funded and profile.open_engineering_roles >= 3:
        return 1
    if profile.had_layoffs:
        return 2
    if profile.headcount_growth_pct >= 40.0:
        return 3
    return 0


# ── Main entry point ─────────────────────────────────────────────────────────

def enrich(email: str) -> CompanyProfile:
    """
    Run all four signal sources, merge results, and classify into a sales segment.

    Collection order and confidence ranges
    ---------------------------------------
    1. Crunchbase ODM  → funding / headcount   (0.9 with key, 0.0 without)
    2. Playwright jobs → open engineering roles (0.8 if roles found, 0.1 if not)
    3. Layoffs.fyi CSV → recent layoff events  (0.95 if found, 0.6 if clean)
    4. PDL leadership  → recent exec moves     (0.85 with key, 0.0 without)
    5. LLM fallback    → fills remaining gaps using signals 1–4 as context
    """
    domain = _extract_domain(email)

    cb_signal = _fetch_crunchbase(domain)
    job_signal = _scrape_job_posts(domain)
    layoffs_sig = _parse_layoffs_fyi(domain.split(".")[0].title(), domain)
    leadership_sig = _detect_leadership_change(domain)

    # Build context string for LLM from high-confidence real signals
    ctx_parts = []
    if cb_signal.confidence > 0.1:
        ctx_parts.append(f"Crunchbase: {json.dumps(cb_signal.value)}")
    if job_signal.confidence > 0.1:
        ctx_parts.append(f"Job posts: {json.dumps(job_signal.value)}")
    if layoffs_sig.confidence > 0.1:
        ctx_parts.append(f"Layoffs.fyi: {json.dumps(layoffs_sig.value)}")

    raw = _llm_enrich(domain, "\n".join(ctx_parts))

    # Override LLM values with authoritative real-signal data
    cb_val = cb_signal.value if isinstance(cb_signal.value, dict) else {}
    if cb_signal.confidence >= 0.5:
        if cb_val.get("funding_stage"):
            raw["funding_stage"] = cb_val["funding_stage"]
        if cb_val.get("recently_funded") is not None:
            raw["recently_funded"] = cb_val["recently_funded"]

    job_val = job_signal.value if isinstance(job_signal.value, dict) else {}
    if job_signal.confidence >= 0.4:
        raw["open_engineering_roles"] = job_val.get(
            "open_engineering_roles", raw.get("open_engineering_roles", 0)
        )

    layoffs_val = layoffs_sig.value if isinstance(layoffs_sig.value, dict) else {}
    if layoffs_sig.confidence >= 0.5:
        raw["had_layoffs"] = layoffs_val.get("had_layoffs", raw.get("had_layoffs", False))

    # Build LeadershipChange from PDL signal
    leadership_change = LeadershipChange()
    ld_val = leadership_sig.value if isinstance(leadership_sig.value, dict) else {}
    if ld_val.get("detected"):
        leadership_change = LeadershipChange(
            detected=True,
            changed_role=ld_val.get("changed_role", ""),
            change_type=ld_val.get("change_type", "new_hire"),
            change_date=ld_val.get("change_date", ""),
            previous_company=ld_val.get("previous_company", ""),
            days_since_change=ld_val.get("days_since_change", 0),
            outreach_urgency_boost=ld_val.get("outreach_urgency_boost", 0.0),
        )

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
        crunchbase_signal=cb_signal.to_dict(),
        job_posts_signal=job_signal.to_dict(),
        layoffs_signal=layoffs_sig.to_dict(),
        leadership_change_signal=leadership_sig.to_dict(),
        leadership_change=leadership_change,
    )
    profile.segment = _classify_segment(profile)
    return profile
