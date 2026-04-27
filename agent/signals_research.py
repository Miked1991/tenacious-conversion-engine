"""
Pre-email signals research via Playwright.

Scrapes the company's public homepage and blog/news pages to extract
personalization hooks used in cold email composition:

  - tagline       : meta description or first h1
  - recent_post   : most recent blog / news / press headline
  - product_hint  : product name from /pricing or /product page title
  - tech_hints    : tech stack guesses from script src domains

The results are stored in CompanyProfile.signals_research and surfaced
as a one-liner via personalization_hook() for the LLM compose step.
"""

import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_TIMEOUT_MS = int(os.getenv("PLAYWRIGHT_TIMEOUT_MS", "8000"))

# Script domains that signal well-known tech stacks
_TECH_PATTERNS: dict[str, str] = {
    "stripe.com": "Stripe",
    "segment.com": "Segment",
    "intercom.io": "Intercom",
    "mixpanel.com": "Mixpanel",
    "amplitude.com": "Amplitude",
    "sentry.io": "Sentry",
    "datadog.com": "Datadog",
    "salesforce.com": "Salesforce",
    "hubspot.com": "HubSpot",
    "algolia.net": "Algolia",
    "cloudflare.com": "Cloudflare",
    "amazonaws.com": "AWS",
    "azure.com": "Azure",
    "googleapis.com": "GCP",
}

# Nav-link text patterns that lead to blog / news / press pages
_BLOG_LINK_RE = re.compile(
    r"\b(blog|news|press|updates|insights|resources|articles)\b",
    re.IGNORECASE,
)


@dataclass
class CompanySignals:
    tagline: str = ""
    recent_post: str = ""
    product_hint: str = ""
    tech_hints: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    error: str = ""

    def personalization_hook(self) -> str:
        """Best one-liner for email personalisation; empty string if nothing found."""
        if self.recent_post:
            return f'Saw your recent post: "{self.recent_post}"'
        if self.tagline:
            return self.tagline[:120]
        return ""

    def to_dict(self) -> dict:
        return {
            "tagline": self.tagline,
            "recent_post": self.recent_post,
            "product_hint": self.product_hint,
            "tech_hints": self.tech_hints,
            "source_urls": self.source_urls,
            "personalization_hook": self.personalization_hook(),
            "error": self.error,
        }


def _extract_tagline(page) -> str:
    """Pull meta description → og:description → first h1, in priority order."""
    for selector, attr in [
        ('meta[name="description"]', "content"),
        ('meta[property="og:description"]', "content"),
    ]:
        try:
            el = page.query_selector(selector)
            if el:
                val = (el.get_attribute(attr) or "").strip()
                if val:
                    return val
        except Exception:
            pass
    try:
        el = page.query_selector("h1")
        if el:
            return (el.inner_text() or "").strip()[:200]
    except Exception:
        pass
    return ""


def _extract_tech_hints(page) -> list[str]:
    """Check script src domains for known tech stack markers."""
    found: list[str] = []
    try:
        handles = page.query_selector_all("script[src]")
        for h in handles:
            src = (h.get_attribute("src") or "").lower()
            for domain, label in _TECH_PATTERNS.items():
                if domain in src and label not in found:
                    found.append(label)
    except Exception:
        pass
    return found


def _find_blog_url(page, base_url: str) -> str:
    """Walk nav links looking for a blog / news / press page."""
    try:
        links = page.query_selector_all("a[href]")
        for link in links[:60]:
            href = (link.get_attribute("href") or "").strip()
            text = (link.inner_text() or "").strip()
            if _BLOG_LINK_RE.search(text):
                if href.startswith("http"):
                    return href
                if href.startswith("/"):
                    return base_url.rstrip("/") + href
    except Exception:
        pass
    return ""


def _extract_recent_post(page) -> str:
    """
    Grab the first article/post headline on a blog or news page.
    Tries <article h2>, <h2>, then <h3> to catch common CMS layouts.
    """
    for selector in ["article h2", "h2", "h3"]:
        try:
            els = page.query_selector_all(selector)
            for el in els[:5]:
                text = (el.inner_text() or "").strip()
                if len(text) > 15:
                    return text[:140]
        except Exception:
            continue
    return ""


def _extract_product_hint(page, domain: str) -> str:
    """
    Visit /pricing or /product to extract the page <title> as a product hint.
    Falls back to empty string if the page is not found.
    """
    for path in ["/pricing", "/product", "/features", "/platform"]:
        try:
            resp = page.goto(
                f"https://{domain}{path}",
                timeout=_TIMEOUT_MS,
                wait_until="domcontentloaded",
            )
            if resp and resp.status == 200:
                title = (page.title() or "").strip()
                title = re.sub(r"\s*[|\-–—].*$", "", title).strip()
                if title:
                    return title[:80]
        except Exception:
            continue
    return ""


def research(domain: str, trace_id: str = "") -> CompanySignals:
    """
    Run Playwright signals research for *domain*.

    Scrapes:
      1. Homepage  → tagline, tech hints, blog link
      2. Blog/news → most recent post headline
      3. /pricing or /product → product hint

    Returns CompanySignals; never raises — errors land in .error field.
    """
    signals = CompanySignals()
    try:
        from playwright.sync_api import sync_playwright, Error as PwError
    except ImportError:
        signals.error = "playwright not installed"
        return signals

    base_url = f"https://{domain}"
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent="Mozilla/5.0 (compatible; TenaciousBot/1.0)"
            )

            # ── Step 1: homepage ─────────────────────────────────────────────
            try:
                resp = page.goto(base_url, timeout=_TIMEOUT_MS, wait_until="domcontentloaded")
                if resp and resp.status < 400:
                    signals.source_urls.append(base_url)
                    signals.tagline = _extract_tagline(page)
                    signals.tech_hints = _extract_tech_hints(page)
                    blog_url = _find_blog_url(page, base_url)

                    # ── Step 2: blog / news page ─────────────────────────────
                    if blog_url:
                        try:
                            bresp = page.goto(
                                blog_url,
                                timeout=_TIMEOUT_MS,
                                wait_until="domcontentloaded",
                            )
                            if bresp and bresp.status < 400:
                                signals.source_urls.append(blog_url)
                                signals.recent_post = _extract_recent_post(page)
                        except (PwError, Exception):
                            pass
            except (PwError, Exception) as exc:
                signals.error = str(exc)

            # ── Step 3: product/pricing hint ─────────────────────────────────
            try:
                signals.product_hint = _extract_product_hint(page, domain)
            except Exception:
                pass

            browser.close()
    except Exception as exc:
        signals.error = str(exc)

    if not signals.tagline and not signals.recent_post:
        logger.debug("signals_research: no content found for %s", domain)

    return signals
