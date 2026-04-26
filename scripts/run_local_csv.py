"""
Run the full pipeline on a single company from the local Crunchbase CSV.

Usage:
    python scripts/run_local_csv.py                        # first company
    python scripts/run_local_csv.py --name "Winder Research"
    python scripts/run_local_csv.py --index 2
    python scripts/run_local_csv.py --dry-run              # enrich only, skip email/SMS/CRM
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

CSV_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                        "data", "crunchbase", "crunchbase-companies-information.csv")


def _load_row(name: str | None, index: int) -> dict:
    with open(CSV_PATH, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        rows = [r for r in reader if r.get("website") and r.get("name")]
    if name:
        matches = [r for r in rows if name.lower() in r["name"].lower()]
        if not matches:
            print(f"[ERROR] No company matching '{name}' in CSV.")
            sys.exit(1)
        return matches[0]
    return rows[index]


def _domain_from_url(url: str) -> str:
    url = url.strip().rstrip("/")
    url = re.sub(r"^https?://", "", url)
    url = re.sub(r"^www\.", "", url)
    return url.split("/")[0]


def _headcount_midpoint(band: str) -> int:
    band = band.strip()
    if not band:
        return 0
    m = re.match(r"(\d+)\s*[-–]\s*(\d+)", band)
    if m:
        return (int(m.group(1)) + int(m.group(2))) // 2
    if band.isdigit():
        return int(band)
    return 0


def _parse_funding_rounds(raw_json: str) -> tuple[str, int, float]:
    """Return (funding_stage, num_rounds, total_usd) from the funding_rounds_list JSON."""
    try:
        rounds = json.loads(raw_json) if raw_json.strip().startswith("[") else []
    except Exception:
        rounds = []
    if not rounds:
        return "", 0, 0.0
    latest = rounds[-1] if isinstance(rounds[-1], dict) else {}
    stage = latest.get("series", "") or latest.get("investment_type", "") or ""
    total = sum(
        float((r.get("money_raised") or {}).get("value_usd") or 0)
        for r in rounds if isinstance(r, dict)
    )
    return stage, len(rounds), total


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--name",    default=None, help="Company name substring to match")
    parser.add_argument("--index",   type=int, default=0, help="Row index (0-based)")
    parser.add_argument("--dry-run", action="store_true", help="Enrich only; skip email/SMS/CRM")
    args = parser.parse_args()

    row = _load_row(args.name, args.index)
    domain  = _domain_from_url(row["website"])
    company = row["name"].strip()
    headcount = _headcount_midpoint(row.get("num_employees", ""))
    funding_stage, num_rounds, total_usd = _parse_funding_rounds(row.get("funding_rounds_list", ""))

    print(f"\n=== Company: {company} ===")
    print(f"    Domain:        {domain}")
    print(f"    Headcount:     {headcount}")
    print(f"    Funding stage: {funding_stage or 'unknown'} ({num_rounds} rounds, ${total_usd:,.0f})")

    # Patch _fetch_crunchbase so it returns CSV data instead of hitting the API
    import agent.enrichment_pipeline as ep

    def _csv_crunchbase(d: str):
        return ep.SignalResult(
            value={
                "domain":           domain,
                "found":            True,
                "funding_stage":    funding_stage,
                "funding_rounds":   num_rounds,
                "total_funding_usd": total_usd,
                "recently_funded":  bool(funding_stage),
                "headcount_band":   row.get("num_employees", ""),
            },
            confidence=0.85,
            source="crunchbase_csv_local",
        )

    ep._fetch_crunchbase = _csv_crunchbase

    # Use a synthetic email built from the domain
    email = f"founder@{domain}"
    print(f"    Synthetic email: {email}\n")

    print("--- Running enrichment pipeline ---")
    profile = ep.enrich(email)

    print(f"    Segment:         {profile.segment}  (0=generic 1=funded 2=layoff 3=hypergrowth)")
    print(f"    AI maturity:     {profile.ai_maturity_score}/3")
    print(f"    Open eng roles:  {profile.open_engineering_roles}")
    print(f"    AI reason:       {profile.raw.get('ai_maturity_reason', 'n/a')}")

    if args.dry_run:
        print("\n[dry-run] Skipping email/CRM/SMS. Full profile:")
        print(json.dumps(profile.__dict__, default=str, indent=2))
        return

    from agent import competitor_gap as gap_mod
    from agent import email_outreach as email_mod
    from agent import conversation_handler as conv
    from agent import hubspot_sync as hs
    from agent import langfuse_logger as lf

    trace_id = lf.log_trace("local_csv_run", {"email": email, "company": company}, None)

    print("\n--- Competitor gap brief ---")
    gap = gap_mod.generate_competitor_gap_brief(profile, trace_id)
    print(f"    Industry:          {gap['industry']}")
    print(f"    Recommended angle: {gap['recommended_angle']}")
    print(f"    Top gap summary:   {gap['top_gap_summary']}")

    # Resend sandbox only allows sending to the verified owner address.
    # Override the recipient so the email actually delivers in test mode.
    TEST_EMAIL = os.getenv("TEST_EMAIL", "mikiasdagem@gmail.com")
    profile.email = TEST_EMAIL

    print(f"\n--- Composing email (to: {TEST_EMAIL}) ---")
    subject, body = email_mod.compose(profile, trace_id)
    print(f"    Subject : {subject}")
    print(f"    Body    :\n{body}\n")
    if not body.strip():
        print("    [WARN] Body is empty — check LLM response format above")
    result = email_mod.compose_and_send(profile, trace_id)
    print(f"    Send result: {result}")

    print("\n--- HubSpot auth check ---")
    import httpx as _httpx
    _hs_token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    _hs_test = _httpx.get(
        "https://api.hubapi.com/crm/v3/objects/contacts?limit=1",
        headers={"Authorization": f"Bearer {_hs_token}"},
        timeout=10,
    )
    print(f"    Status: {_hs_test.status_code}  (200=OK, 401=bad token, 403=missing scope)")
    if _hs_test.status_code != 200:
        print(f"    Response: {_hs_test.text[:300]}")

    print("\n--- Upserting to HubSpot ---")
    from agent.email_outreach import SEGMENT_LABELS
    from datetime import datetime, timezone
    name_parts = company.split()
    first = name_parts[0] if name_parts else "Founder"
    last  = name_parts[-1] if len(name_parts) > 1 else ""
    contact_id = hs.upsert_contact(
        email=TEST_EMAIL,
        first_name=first,
        last_name=last,
        company=company,
        segment_label=SEGMENT_LABELS.get(profile.segment, "generic"),
        ai_maturity_score=profile.ai_maturity_score,
        booking_url="",
        enrichment_ts=datetime.now(timezone.utc).isoformat(),
        trace_id=trace_id,
    )
    if contact_id:
        print(f"    HubSpot contact ID: {contact_id}")
        print(f"    View at: https://app.hubspot.com/contacts/search?query={TEST_EMAIL}")
    else:
        # Re-run the upsert with raw httpx to see the actual error
        _payload = {
            "properties": {
                "email": TEST_EMAIL,
                "firstname": first,
                "lastname": last,
                "company": company,
            }
        }
        _raw = _httpx.post(
            "https://api.hubapi.com/crm/v3/objects/contacts",
            headers={"Authorization": f"Bearer {_hs_token}", "Content-Type": "application/json"},
            json=_payload,
            timeout=10,
        )
        print(f"    [DEBUG] Raw upsert status: {_raw.status_code}")
        print(f"    [DEBUG] Raw upsert response: {_raw.text[:400]}")

    print("\n=== Done ===")
    print(f"Check Langfuse trace: trace_id={trace_id}")


if __name__ == "__main__":
    main()
