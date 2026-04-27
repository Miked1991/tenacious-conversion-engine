"""
Conversion Engine Interactive Dashboard
Run: python dashboard.py
Open: http://localhost:8001
"""
import csv
import dataclasses as _dc
import io
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv()
app = FastAPI(title="Conversion Engine Dashboard")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/batch-results")
def get_batch_results():
    results_dir = BASE_DIR / "eval" / "crunchbase-result"
    results = []
    if results_dir.exists():
        files = sorted(results_dir.glob("crunchbase_*.json"),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        for jf in files:
            if "summary" not in jf.name:
                try:
                    data = json.loads(jf.read_text(encoding="utf-8"))
                    data["_slug"] = jf.stem
                    results.append(data)
                except Exception:
                    pass
    return JSONResponse(results)


@app.get("/api/db-leads")
def get_db_leads():
    db_path = BASE_DIR / "leads.db"
    if not db_path.exists():
        return JSONResponse([])
    try:
        from sqlalchemy import create_engine, text
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT * FROM leads ORDER BY COALESCE(updated_at,'') DESC, created_at DESC")
            ).mappings()
            leads = []
            for row in rows:
                lead = dict(row)
                lead["profile"] = json.loads(lead.pop("profile_json", None) or "{}")
                lead["history"] = json.loads(lead.pop("history_json", None) or "[]")
                leads.append(lead)
        return JSONResponse(leads)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.get("/api/traces")
def get_traces():
    tf = BASE_DIR / "held_out_traces.jsonl"
    if not tf.exists():
        return JSONResponse([])
    traces = []
    for line in tf.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                traces.append(json.loads(line))
            except Exception:
                pass
    return JSONResponse(traces)


class _EmailBody(BaseModel):
    email: str


@app.get("/api/debug-calcom")
def debug_calcom():
    import httpx as _hx
    url  = os.getenv("CALCOM_API_URL", "http://localhost:3000")
    key  = os.getenv("CALCOM_API_KEY", "")
    slug = os.getenv("CALCOM_EVENT_TYPE_SLUG", "discovery-call")
    if not key:
        return JSONResponse({"error": "CALCOM_API_KEY not set", "url": url, "slug": slug})
    is_cloud = "api.cal.com" in url
    # probe v2 for cloud, v1 for self-hosted
    if is_cloud:
        h2 = {"Authorization": f"Bearer {key}", "cal-api-version": "2024-08-13"}
        results = {}
        # Get username from /v2/me first
        username = None
        user_id  = None
        try:
            me = _hx.get("https://api.cal.com/v2/me", headers=h2, timeout=10).json()
            username = me.get("data", {}).get("username", "")
            user_id  = me.get("data", {}).get("id", "")
            results["/v2/me"] = {"status": 200, "username": username, "id": user_id}
        except Exception as exc:
            results["/v2/me"] = {"error": str(exc)}
        # Probe event-type paths
        probes = {
            "v2 by username":  f"https://api.cal.com/v2/event-types?username={username}",
            "v2 by user id":   f"https://api.cal.com/v2/users/{user_id}/event-types",
            "v1 apiKey api":   f"https://api.cal.com/v1/event-types?apiKey={key}",
            "v1 apiKey app":   f"https://app.cal.com/api/v1/event-types?apiKey={key}",
            "public user page":f"https://cal.com/api/trpc/public/event-types?batch=1&input=%7B%220%22%3A%7B%22username%22%3A%22{username}%22%7D%7D",
        }
        for label, full_url in probes.items():
            try:
                r = _hx.get(full_url, timeout=10)
                d = r.json()
                if isinstance(d, list):
                    d = d[0] if d else {}
                ets = (d.get("result", {}).get("data", {}).get("eventTypes")
                       or d.get("event_types") or d.get("data") or [])
                if isinstance(ets, list):
                    ets_summary = [{"id": e.get("id"), "slug": e.get("slug"), "title": e.get("title")} for e in ets[:10]]
                else:
                    ets_summary = str(d)[:300]
                results[label] = {"status": r.status_code, "event_types": ets_summary}
            except Exception as exc:
                results[label] = {"error": str(exc)}
        et_id_env = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        return JSONResponse({
            "api": "v2", "username": username, "user_id": user_id,
            "slug_target": slug,
            "CALCOM_EVENT_TYPE_ID_set": bool(et_id_env),
            "CALCOM_EVENT_TYPE_ID": et_id_env or None,
            "fix_if_missing": (
                "Visit app.cal.com/event-types, open your event type, "
                "copy the number from the URL (e.g. /event-types/12345), "
                "then set CALCOM_EVENT_TYPE_ID=12345 in .env and restart."
            ),
            "probes": results,
        })
    else:
        try:
            r = _hx.get(f"{url}/api/v1/event-types",
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
            data = r.json()
            ets = [{"id": e.get("id"), "slug": e.get("slug"), "title": e.get("title")}
                   for e in data.get("event_types", [])]
            return JSONResponse({"api": "v1", "url": url, "slug_target": slug,
                                 "status": r.status_code, "event_types": ets})
        except Exception as exc:
            return JSONResponse({"api": "v1", "error": str(exc)})


def _book_calcom(email: str, name: str, trace_id: str) -> dict:
    """Book via Cal.com v2 (cloud api.cal.com) or v1 (self-hosted)."""
    import httpx as _hx, time as _t
    from datetime import datetime, timezone as _tz

    url  = os.getenv("CALCOM_API_URL", "http://localhost:3000")
    key  = os.getenv("CALCOM_API_KEY", "")
    slug = os.getenv("CALCOM_EVENT_TYPE_SLUG", "discovery-call")
    biz_s = int(os.getenv("BIZ_HOUR_START", "9"))
    biz_e = int(os.getenv("BIZ_HOUR_END", "17"))

    if not key:
        return {"success": False, "booking_url": "", "slot": "", "error": "CALCOM_API_KEY not set"}

    def _biz(iso: str) -> bool:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            return biz_s <= dt.hour < biz_e
        except Exception:
            return False

    if "api.cal.com" in url:
        base = "https://api.cal.com/v2"
        hdrs = {"Authorization": f"Bearer {key}", "cal-api-version": "2024-08-13",
                "Content-Type": "application/json"}
        try:
            r = _hx.get(f"{base}/event-types", headers=hdrs, timeout=10)
            ets = r.json().get("data", [])
        except Exception as exc:
            return {"success": False, "booking_url": "", "slot": "", "error": str(exc)}

        # Direct override: CALCOM_EVENT_TYPE_ID skips the listing step
        et_id_env = os.getenv("CALCOM_EVENT_TYPE_ID", "")
        if et_id_env:
            et_id = int(et_id_env)
        else:
            et_id = next((e["id"] for e in ets if e.get("slug") == slug), None)
        if not et_id:
            return {"success": False, "booking_url": "", "slot": "",
                    "error": (
                        "Event type not found. "
                        "Open app.cal.com/event-types, click your event type, "
                        "copy the numeric ID from the URL, then add "
                        "CALCOM_EVENT_TYPE_ID=<id> to your .env and restart."
                    )}

        now = _t.strftime("%Y-%m-%dT00:00:00.000Z", _t.gmtime())
        end = _t.strftime("%Y-%m-%dT23:59:59.000Z", _t.gmtime(_t.time() + 7 * 86400))
        try:
            r = _hx.get(f"{base}/slots/available",
                        params={"startTime": now, "endTime": end, "eventTypeId": et_id},
                        headers=hdrs, timeout=15)
            slots_by_day = r.json().get("data", {}).get("slots", {})
        except Exception as exc:
            return {"success": False, "booking_url": "", "slot": "", "error": str(exc)}

        slot = next((s["time"] for day in slots_by_day.values()
                     for s in day if _biz(s.get("time", ""))), None)
        if not slot:
            return {"success": False, "booking_url": "", "slot": "",
                    "error": "no available business-hours slots in next 7 days"}

        try:
            r = _hx.post(f"{base}/bookings", headers=hdrs, timeout=20, json={
                "eventTypeId": et_id,
                "start": slot,
                "attendee": {"name": name or email.split("@")[0].title(),
                             "email": email, "timeZone": "UTC", "language": "en"},
            })
            data = r.json()
            uid = data.get("data", {}).get("uid", "")
            booking_url = f"https://cal.com/booking/{uid}" if uid else ""
            return {"success": bool(uid), "booking_url": booking_url, "slot": slot,
                    "_raw": data.get("data", {})}
        except Exception as exc:
            return {"success": False, "booking_url": "", "slot": slot, "error": str(exc)}
    else:
        from agent import booking_handler
        return booking_handler.book(email, name, trace_id, key)


@app.post("/api/sync-booking/{slug}")
def sync_booking(slug: str):
    jf = BASE_DIR / "eval" / "crunchbase-result" / f"{slug}.json"
    if not jf.exists():
        return JSONResponse({"success": False, "error": "lead not found"}, status_code=404)
    data     = json.loads(jf.read_text(encoding="utf-8"))
    email    = data.get("input", {}).get("contact_email", "")
    name     = data.get("input", {}).get("name", "")
    trace_id = data.get("trace_id", str(uuid.uuid4()))
    result   = _book_calcom(email, name, trace_id)
    data["booking"] = result
    jf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return JSONResponse(result)


@app.post("/api/sync-crm/{slug}")
def sync_crm(slug: str):
    jf = BASE_DIR / "eval" / "crunchbase-result" / f"{slug}.json"
    if not jf.exists():
        return JSONResponse({"success": False, "error": "lead not found"}, status_code=404)
    data      = json.loads(jf.read_text(encoding="utf-8"))
    email     = data.get("input", {}).get("contact_email", "")
    name      = data.get("input", {}).get("name", "")
    enrich    = data.get("enrichment", {})
    company   = enrich.get("company_name", name)
    seg       = data.get("segment", {}).get("label", "generic")
    ai_score  = int(data.get("ai_maturity", {}).get("score", 0) or 0)
    book_url  = data.get("booking", {}).get("booking_url", "")
    enrich_ts = enrich.get("enriched_at", "")
    trace_id  = data.get("trace_id", str(uuid.uuid4()))
    parts = name.split()
    first, last = (parts[0], " ".join(parts[1:])) if parts else ("", "")
    contact_id, hs_error = _hs_upsert_with_error(
        email, first, last, company, seg, ai_score, book_url, enrich_ts, trace_id
    )
    if not contact_id:
        return JSONResponse({"success": False, "error": hs_error or "HubSpot returned no ID",
                             "contact_id": ""})
    data.setdefault("hubspot", {})["contact_id"] = contact_id
    jf.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return JSONResponse({"success": True, "contact_id": contact_id})


@app.post("/api/sync-booking-db")
def sync_booking_db(body: _EmailBody):
    db_path = BASE_DIR / "leads.db"
    if not db_path.exists():
        return JSONResponse({"success": False, "error": "no database"})
    try:
        from sqlalchemy import create_engine, text as sql
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        with engine.connect() as conn:
            row = conn.execute(sql("SELECT * FROM leads WHERE email=:e"), {"e": body.email}).mappings().first()
        if not row:
            return JSONResponse({"success": False, "error": "lead not found"})
        profile  = json.loads(row.get("profile_json") or "{}")
        company  = profile.get("company_name", body.email.split("@")[0].title())
        trace_id = row.get("lead_id", str(uuid.uuid4()))
        result = _book_calcom(body.email, company, trace_id)
        if result.get("success"):
            with engine.connect() as conn:
                conn.execute(
                    sql("UPDATE leads SET booking_url=:u, updated_at=datetime('now') WHERE email=:e"),
                    {"u": result["booking_url"], "e": body.email},
                )
                conn.commit()
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"success": False, "error": str(exc)})


@app.post("/api/sync-crm-db")
def sync_crm_db(body: _EmailBody):
    db_path = BASE_DIR / "leads.db"
    if not db_path.exists():
        return JSONResponse({"success": False, "error": "no database"})
    try:
        from sqlalchemy import create_engine, text as sql
        engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
        with engine.connect() as conn:
            row = conn.execute(sql("SELECT * FROM leads WHERE email=:e"), {"e": body.email}).mappings().first()
        if not row:
            return JSONResponse({"success": False, "error": "lead not found"})
        profile   = json.loads(row.get("profile_json") or "{}")
        name      = profile.get("company_name", "")
        company   = profile.get("company_name", "")
        seg       = str(profile.get("segment", "generic"))
        ai_score  = int(profile.get("ai_maturity_score", 0) or 0)
        book_url  = row.get("booking_url", "") or ""
        enrich_ts = profile.get("enriched_at", "")
        trace_id  = row.get("lead_id", str(uuid.uuid4()))
        parts = name.split()
        first, last = (parts[0], " ".join(parts[1:])) if parts else ("", "")
        contact_id, hs_error = _hs_upsert_with_error(
            body.email, first, last, company, seg, ai_score, book_url, enrich_ts, trace_id
        )
        if not contact_id:
            return JSONResponse({"success": False,
                                 "error": hs_error or "HubSpot returned no ID",
                                 "contact_id": ""})
        with engine.connect() as conn:
            conn.execute(
                sql("UPDATE leads SET hubspot_contact_id=:c, updated_at=datetime('now') WHERE email=:e"),
                {"c": contact_id, "e": body.email},
            )
            conn.commit()
        return JSONResponse({"success": True, "contact_id": contact_id})
    except Exception as exc:
        return JSONResponse({"success": False, "error": str(exc), "contact_id": ""})


# ── CSV / live-pipeline ──────────────────────────────────────────────────────

_JOBS: dict = {}
_JOBS_LOCK  = threading.Lock()


class _PipelineBody(BaseModel):
    rows:  list[dict]
    limit: int = 10


def _hs_upsert_with_error(email, first, last, company, seg, ai, book, ts, trace_id):
    """Wrapper around hubspot_sync.upsert_contact that surfaces HTTP errors."""
    import httpx as _hx
    token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        return "", "HUBSPOT_ACCESS_TOKEN not set"
    base    = "https://api.hubapi.com"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    props   = {
        "email": email, "firstname": first, "lastname": last, "company": company,
        "hs_lead_status": "IN_PROGRESS",
        "message": f"segment={seg} | ai_maturity={ai} | booking={book} | enriched={ts}",
    }
    try:
        # Search for existing contact
        sr = _hx.post(f"{base}/crm/v3/objects/contacts/search", headers=headers,
                      json={"filterGroups":[{"filters":[{"propertyName":"email",
                            "operator":"EQ","value":email}]}],"properties":["email"],"limit":1},
                      timeout=15)
        existing = (sr.json().get("results") or [{}])[0].get("id") if sr.status_code == 200 else None
        if existing:
            r = _hx.patch(f"{base}/crm/v3/objects/contacts/{existing}", headers=headers,
                          json={"properties": props}, timeout=15)
        else:
            r = _hx.post(f"{base}/crm/v3/objects/contacts", headers=headers,
                         json={"properties": props}, timeout=15)
        if r.status_code in (200, 201):
            return r.json().get("id", ""), ""
        err_body = r.json()
        return "", f"HTTP {r.status_code}: {err_body.get('message', str(err_body)[:120])}"
    except Exception as exc:
        return "", str(exc)[:200]


def _run_full_pipeline_row(email: str, name: str, on_step=None) -> dict:
    """8-step pipeline: Enrich → Signals → Segment → Competitor Gap → Email Compose → Email Send → Booking → CRM."""
    from agent import enrichment_pipeline as _ep
    from agent import email_outreach      as _eo
    from agent import competitor_gap      as _gap
    from agent import signals_research    as _sr
    from agent import langfuse_logger     as _lf
    from agent import db                  as _db

    trace_id = _lf.log_trace("csv_pipeline", {"email": email}, None)
    steps: list[dict] = []

    def _step(step_name: str, output: dict, error: str = "") -> None:
        steps.append({
            "step":   step_name,
            "status": "error" if error else "done",
            "output": output,
            "error":  error,
            "ts":     time.time(),
        })
        if on_step:
            on_step(step_name)

    # ── Step 1: Enrichment ────────────────────────────────────────────────────
    profile = None
    profile_d: dict = {}
    try:
        profile   = _ep.enrich(email)
        profile_d = _dc.asdict(profile)
        _lf.log_span(trace_id, "enrich", {"email": email}, profile_d)
        lc = profile.leadership_change
        _step("Enrichment", {
            "company_name":          profile.company_name,
            "domain":                profile.domain,
            "headcount":             profile.headcount,
            "funding_stage":         profile.funding_stage,
            "recently_funded":       profile.recently_funded,
            "had_layoffs":           profile.had_layoffs,
            "open_engineering_roles":profile.open_engineering_roles,
            "ai_maturity_score":     profile.ai_maturity_score,
            "crunchbase_signal":     profile.crunchbase_signal,
            "job_posts_signal":      profile.job_posts_signal,
            "layoffs_signal":        profile.layoffs_signal,
            "leadership_change_signal": profile.leadership_change_signal,
            "leadership_change":     _dc.asdict(lc) if lc else None,
            "enriched_at":           profile.enriched_at,
        })
    except Exception as exc:
        _step("Enrichment", {}, str(exc)[:300])
        return {"email": email, "company_name": email, "steps": steps, "error": str(exc)[:300]}

    # ── Step 2: Signals Research (Playwright) ─────────────────────────────────
    company_signals = None
    try:
        company_signals = _sr.research(profile.domain, trace_id)
        profile.signals_research = company_signals.to_dict()
        _lf.log_span(trace_id, "signals_research", {"domain": profile.domain}, profile.signals_research)
        _step("Signals Research", {
            "tagline":             company_signals.tagline,
            "recent_post":         company_signals.recent_post,
            "product_hint":        company_signals.product_hint,
            "tech_hints":          company_signals.tech_hints,
            "personalization_hook":company_signals.personalization_hook(),
            "source_urls":         company_signals.source_urls,
            "error":               company_signals.error,
        })
    except Exception as exc:
        _step("Signals Research", {"error": str(exc)[:200]})

    # ── Step 3: Segment ───────────────────────────────────────────────────────
    seg_id    = profile.segment
    seg_label = _eo.SEGMENT_LABELS.get(seg_id, "generic")
    lc2 = profile.leadership_change
    _step("Segment", {
        "segment_id":               seg_id,
        "segment_label":            seg_label,
        "ai_maturity_score":        profile.ai_maturity_score,
        "recently_funded":          profile.recently_funded,
        "had_layoffs":              profile.had_layoffs,
        "open_engineering_roles":   profile.open_engineering_roles,
        "leadership_change_detected": bool(lc2 and lc2.detected),
    })

    # ── Step 4: Competitor Gap ────────────────────────────────────────────────
    gap_brief: dict = {}
    try:
        gap_brief = _gap.generate_competitor_gap_brief(profile, trace_id)
        _step("Competitor Gap", gap_brief if isinstance(gap_brief, dict) else {"brief": str(gap_brief)[:500]})
    except Exception as exc:
        _step("Competitor Gap", {}, str(exc)[:300])

    # ── Step 5: Email Compose ─────────────────────────────────────────────────
    subject, body = "", ""
    det_ok, violations, tone_ok = True, [], True
    try:
        subject, body = _eo.compose(profile, trace_id)
        det_ok, violations = _eo._deterministic_tone_check(subject, body)
        if not det_ok:
            subject, body = _eo.compose(profile, trace_id)
            det_ok, violations = _eo._deterministic_tone_check(subject, body)
        tone_ok    = _eo.tone_check(subject, body, trace_id)
        job_conf   = (profile.job_posts_signal or {}).get("confidence", 0.5)
        cb_conf    = (profile.crunchbase_signal or {}).get("confidence", 0.5)
        avg_conf   = round((job_conf + cb_conf) / 2, 3)
        _step("Email Compose", {
            "subject":              subject,
            "body":                 body,
            "det_ok":               det_ok,
            "violations":           violations,
            "tone_check":           tone_ok,
            "signal_confidence_avg":avg_conf,
        })
    except Exception as exc:
        _step("Email Compose", {"subject": subject, "body": body}, str(exc)[:300])

    # ── Step 6: Email Send ────────────────────────────────────────────────────
    email_sent, email_id, send_error = False, "", ""
    try:
        send_result = _eo.send(email, subject, body, trace_id)
        email_id    = send_result.get("id", "")
        email_sent  = bool(email_id and "error" not in send_result
                           and send_result.get("statusCode", 200) < 300)
        if not email_sent:
            send_error = (send_result.get("error")
                          or send_result.get("message")
                          or f"HTTP {send_result.get('statusCode','?')}")
        _step("Email Send", {
            "sent":          email_sent,
            "resend_id":     email_id,
            "to":            email,
            "outbound_live": _eo._OUTBOUND_LIVE,
            "routed_to":     email if _eo._OUTBOUND_LIVE else _eo._STAFF_SINK,
            "error":         send_error,
        })
    except Exception as exc:
        send_error = str(exc)[:300]
        _step("Email Send", {"sent": False, "error": send_error})

    # ── Step 7: Booking ───────────────────────────────────────────────────────
    booking_result: dict = {"success": False, "booking_url": "", "slot": ""}
    try:
        booking_result = _book_calcom(email, name or profile.company_name, trace_id)
        _step("Booking", {
            "success":     booking_result.get("success", False),
            "slot":        booking_result.get("slot", ""),
            "booking_url": booking_result.get("booking_url", ""),
            "error":       booking_result.get("error", ""),
        })
    except Exception as exc:
        _step("Booking", {"success": False, "error": str(exc)[:300]})

    # ── Step 8: CRM Sync (HubSpot) ────────────────────────────────────────────
    parts    = (name or email.split("@")[0]).replace(".", " ").split()
    first    = parts[0].title() if parts else ""
    last     = " ".join(parts[1:]).title() if len(parts) > 1 else ""
    book_url = booking_result.get("booking_url", "")
    hs_fields = {
        "email":          email,
        "firstname":      first,
        "lastname":       last,
        "company":        profile.company_name,
        "hs_lead_status": "IN_PROGRESS",
        "message":        f"segment={seg_label} | ai_maturity={profile.ai_maturity_score} | booking={book_url}",
    }
    contact_id, hs_error = _hs_upsert_with_error(
        email, first, last, profile.company_name, seg_label,
        profile.ai_maturity_score, book_url, profile.enriched_at, trace_id,
    )
    _step("CRM Sync", {
        "contact_id": contact_id,
        "error":      hs_error,
        "fields":     hs_fields,
    })

    # ── DB save ───────────────────────────────────────────────────────────────
    lead                    = _db.get_or_create(email)
    lead.status             = "outreach_sent"
    lead.profile            = {**profile_d, "competitor_gap_brief": gap_brief}
    lead.hubspot_contact_id = contact_id
    _db.save_lead(lead)

    _lf.log_trace("csv_pipeline_complete", {"email": email}, {"steps": len(steps)},
                  session_id=lead.lead_id)
    return {
        "email":              email,
        "company_name":       profile.company_name,
        "segment":            seg_label,
        "ai_maturity_score":  profile.ai_maturity_score,
        "email_subject":      subject,
        "email_body":         body,
        "email_sent":         email_sent,
        "email_id":           email_id,
        "send_error":         send_error,
        "hubspot_contact_id": contact_id,
        "hs_error":           hs_error,
        "gap_angle":          (gap_brief.get("recommended_angle", "")
                               if isinstance(gap_brief, dict) else ""),
        "booking_success":    booking_result.get("success", False),
        "booking_url":        booking_result.get("booking_url", ""),
        "steps":              steps,
    }


def _run_job(job_id: str) -> None:
    def _log(msg: str) -> None:
        with _JOBS_LOCK:
            _JOBS[job_id]["log"].append({"ts": time.time(), "msg": msg})

    with _JOBS_LOCK:
        n = len(_JOBS[job_id]["companies"])

    for i in range(n):
        with _JOBS_LOCK:
            row   = _JOBS[job_id]["companies"][i]
            email = row["email"]
            _JOBS[job_id]["companies"][i]["status"]       = "running"
            _JOBS[job_id]["companies"][i]["current_step"] = "Starting…"
        _log(f"⟳  Processing {email}…")

        def _on_step(step_name: str, _i=i) -> None:
            with _JOBS_LOCK:
                _JOBS[job_id]["companies"][_i]["current_step"] = step_name

        try:
            result = _run_full_pipeline_row(email, row.get("name", ""), on_step=_on_step)
            with _JOBS_LOCK:
                _JOBS[job_id]["companies"][i]["status"]       = "done"
                _JOBS[job_id]["companies"][i]["current_step"] = ""
                _JOBS[job_id]["companies"][i]["result"]       = result
                _JOBS[job_id]["companies"][i]["steps"]        = result.get("steps", [])
            cname = result.get("company_name") or email
            sent  = "email ✓" if result.get("email_sent") else "email ✗"
            hs    = f"CRM #{result['hubspot_contact_id']}" if result.get("hubspot_contact_id") else "CRM ✗"
            book  = "booked ✓" if result.get("booking_success") else "booked ✗"
            _log(f"✓  {cname} — {sent} · {book} · {hs}")
        except Exception as exc:
            with _JOBS_LOCK:
                _JOBS[job_id]["companies"][i]["status"]       = "error"
                _JOBS[job_id]["companies"][i]["current_step"] = ""
                _JOBS[job_id]["companies"][i]["error"]        = str(exc)[:300]
            _log(f"✗  {email} — {str(exc)[:80]}")

    with _JOBS_LOCK:
        done_n = sum(1 for c in _JOBS[job_id]["companies"] if c["status"] == "done")
        err_n  = sum(1 for c in _JOBS[job_id]["companies"] if c["status"] == "error")
        _JOBS[job_id]["status"]      = "done"
        _JOBS[job_id]["finished_at"] = time.time()
    _log(f"🏁  Done — {done_n} succeeded, {err_n} failed")


@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except Exception:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    rows = []
    for row in reader:
        norm = {k.strip().lower().replace(" ", "_"): (v or "").strip()
                for k, v in row.items()}
        email = (norm.get("email") or norm.get("contact_email")
                 or norm.get("email_address") or "")
        if not email and norm.get("domain"):
            email = f"contact@{norm['domain']}"
        company = (norm.get("company_name") or norm.get("company")
                   or norm.get("organization") or "")
        name    = (norm.get("contact_name") or norm.get("name")
                   or norm.get("first_name") or "")
        rows.append({"email": email, "company": company, "name": name})
    return JSONResponse(rows[:500])


@app.get("/api/debug-hubspot")
def debug_hubspot():
    import httpx as _hx
    token = os.getenv("HUBSPOT_ACCESS_TOKEN", "")
    if not token:
        return JSONResponse({"error": "HUBSPOT_ACCESS_TOKEN not set"})
    base    = "https://api.hubapi.com"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = _hx.get(f"{base}/crm/v3/objects/contacts?limit=1", headers=headers, timeout=10)
        return JSONResponse({
            "status":      r.status_code,
            "token_prefix": token[:12] + "…",
            "response":    r.json(),
            "ok":          r.status_code == 200,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)})


@app.post("/api/start-pipeline")
def start_pipeline(body: _PipelineBody):
    job_id    = str(uuid.uuid4())
    seen      = set()
    companies = []
    for row in body.rows[: body.limit]:
        email = (row.get("email") or "").strip()
        if not email or email in seen:
            continue
        seen.add(email)
        companies.append({
            "email":        email,
            "company":      row.get("company", ""),
            "name":         row.get("name", ""),
            "status":       "pending",
            "result":       None,
            "error":        "",
            "current_step": "",
            "steps":        [],
        })
    with _JOBS_LOCK:
        # Remove old completed jobs to keep memory clean; keep only running ones
        stale = [k for k, v in _JOBS.items() if v["status"] == "done"]
        for k in stale:
            del _JOBS[k]
        _JOBS[job_id] = {
            "status":      "running",
            "companies":   companies,
            "started_at":  time.time(),
            "finished_at": None,
            "log":         [],
        }
    threading.Thread(target=_run_job, args=(job_id,), daemon=True).start()
    return JSONResponse({"job_id": job_id, "count": len(companies)})


@app.get("/api/pipeline-status/{job_id}")
def pipeline_status(job_id: str):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if not job:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return JSONResponse(dict(job))


@app.get("/api/pipeline-jobs")
def pipeline_jobs():
    with _JOBS_LOCK:
        summary = [
            {
                "job_id":     k,
                "status":     v["status"],
                "total":      len(v["companies"]),
                "done":       sum(1 for c in v["companies"] if c["status"] == "done"),
                "errors":     sum(1 for c in v["companies"] if c["status"] == "error"),
                "started_at": v["started_at"],
            }
            for k, v in _JOBS.items()
        ]
    return JSONResponse(sorted(summary, key=lambda x: -x["started_at"])[:20])


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tenacious · Conversion Dashboard</title>
<script src="https://cdn.tailwindcss.com"></script>
<script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js"></script>
<style>
[x-cloak]{display:none!important}
.bubble-agent{background:#052e16;border-left:3px solid #22c55e}
.bubble-prospect{background:#0f172a;border-left:3px solid #3b82f6}
.bar{transition:width .5s ease}
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:#111827}
::-webkit-scrollbar-thumb{background:#374151;border-radius:3px}
</style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen" x-data="app()" x-init="init()" x-cloak>

<!-- TOAST -->
<div x-show="toast" x-transition:enter="transition ease-out duration-200" x-transition:enter-start="opacity-0 translate-y-2" x-transition:enter-end="opacity-100 translate-y-0"
  class="fixed bottom-6 right-6 z-50 px-5 py-3 rounded-xl shadow-2xl text-sm font-semibold max-w-xs"
  :class="toast?.type==='success' ? 'bg-green-600 text-white' : 'bg-red-600 text-white'"
  x-text="toast?.message"></div>

<!-- HEADER -->
<header class="bg-gray-900 border-b border-gray-800 px-5 py-3 flex items-center justify-between sticky top-0 z-50">
  <div class="flex items-center gap-3">
    <div class="w-7 h-7 bg-green-500 rounded-md flex items-center justify-center font-black text-black text-xs">T</div>
    <span class="font-bold">Tenacious</span>
    <span class="text-gray-500 text-sm hidden sm:block">Conversion Engine</span>
    <div class="flex items-center gap-1.5 ml-2">
      <div class="w-1.5 h-1.5 rounded-full bg-green-500 animate-pulse"></div>
      <span class="text-gray-500 text-xs">Live</span>
    </div>
  </div>
  <div class="flex items-center gap-3">
    <span class="text-gray-600 text-xs" x-text="lastUpdated"></span>
    <button @click="refresh()" :disabled="loading"
      class="bg-blue-600 hover:bg-blue-500 disabled:opacity-50 text-white text-xs px-3 py-1.5 rounded-lg transition-colors">
      <span x-text="loading ? 'Loading…' : 'Refresh'"></span>
    </button>
  </div>
</header>

<!-- STATS BAR -->
<div class="bg-gray-900 border-b border-gray-800 px-5 py-3">
  <div class="grid grid-cols-4 sm:grid-cols-7 gap-3">
    <template x-for="s in stats" :key="s.label">
      <div class="text-center">
        <div class="text-xl font-bold" :class="s.color" x-text="s.value"></div>
        <div class="text-xs text-gray-500 mt-0.5" x-text="s.label"></div>
      </div>
    </template>
  </div>
</div>

<!-- TABS -->
<div class="bg-gray-900 border-b border-gray-800 px-5 flex gap-0">
  <template x-for="t in tabs" :key="t.id">
    <button @click="activeTab=t.id; selectedLead=null; selectedDbLead=null; selectedTrace=null"
      class="px-4 py-2.5 text-sm font-medium border-b-2 transition-colors"
      :class="activeTab===t.id ? 'border-green-500 text-green-400' : 'border-transparent text-gray-400 hover:text-gray-200'">
      <span x-text="t.label"></span>
      <span class="ml-1.5 bg-gray-700 text-gray-300 text-xs px-1.5 py-0.5 rounded-full" x-text="t.count"></span>
    </button>
  </template>
</div>

<!-- MAIN: 2-COLUMN LAYOUT -->
<div class="flex" style="height:calc(100vh - 152px)">

  <!-- LEFT LIST -->
  <div class="w-72 shrink-0 border-r border-gray-800 flex flex-col bg-gray-900">
    <div class="p-3 border-b border-gray-800 space-y-2">
      <input x-model="search" type="text" placeholder="Search…"
        class="w-full bg-gray-800 border border-gray-700 text-gray-100 text-xs px-3 py-1.5 rounded-lg focus:outline-none focus:border-blue-500"/>
      <div class="flex gap-1.5">
        <select x-model="filterStatus" class="flex-1 bg-gray-800 border border-gray-700 text-gray-300 text-xs px-2 py-1 rounded-lg">
          <option value="">All Status</option>
          <option value="new">New</option>
          <option value="outreach_sent">Outreach Sent</option>
          <option value="in_conversation">In Conversation</option>
          <option value="qualified">Qualified</option>
          <option value="disqualified">Disqualified</option>
          <option value="booked">Booked</option>
        </select>
        <select x-model="filterSegment" class="flex-1 bg-gray-800 border border-gray-700 text-gray-300 text-xs px-2 py-1 rounded-lg">
          <option value="">All Segments</option>
          <option value="generic">Generic</option>
          <option value="recently_funded">Funded</option>
          <option value="restructuring_cost">Restructuring</option>
          <option value="leadership_transition">Leadership</option>
          <option value="capability_gap">Capability Gap</option>
        </select>
      </div>
    </div>

    <div class="flex-1 overflow-y-auto">

      <!-- PIPELINE LIST -->
      <template x-if="activeTab==='pipeline'">
        <div>
          <template x-for="lead in filteredBatch" :key="lead._slug">
            <div @click="selectedLead=lead" class="p-3 border-b border-gray-800 cursor-pointer transition-colors"
              :class="selectedLead?._slug===lead._slug ? 'bg-gray-800' : 'hover:bg-gray-800/40'">
              <div class="flex items-center justify-between mb-1">
                <span class="font-semibold text-sm truncate" x-text="lead.input?.name || lead.enrichment?.company_name || '—'"></span>
                <span class="text-xs px-1.5 py-0.5 rounded-full shrink-0 ml-1" :class="statusBadge(lead._status)" x-text="lead._status?.replace('_',' ')"></span>
              </div>
              <div class="text-xs text-gray-500 truncate mb-1.5" x-text="lead.input?.contact_email || lead.input?.domain"></div>
              <div class="flex flex-wrap gap-1">
                <span class="text-xs px-1 py-0.5 bg-blue-900/40 text-blue-300 rounded" x-text="'AI '+( lead.ai_maturity?.score ?? '?')"></span>
                <span class="text-xs px-1 py-0.5 bg-purple-900/40 text-purple-300 rounded capitalize" x-text="lead.segment?.label||'generic'"></span>
                <span x-show="lead.conversation?.qualified" class="text-xs px-1 py-0.5 bg-green-900/40 text-green-300 rounded">Qualified</span>
                <span x-show="lead.booking?.success" class="text-xs px-1 py-0.5 bg-yellow-900/40 text-yellow-300 rounded">Booked</span>
              </div>
            </div>
          </template>
          <div x-show="filteredBatch.length===0" class="p-8 text-center text-gray-600 text-sm">No results</div>
        </div>
      </template>

      <!-- LIVE LIST -->
      <template x-if="activeTab==='live'">
        <div>
          <template x-for="lead in filteredDb" :key="lead.email">
            <div @click="selectedDbLead=lead" class="p-3 border-b border-gray-800 cursor-pointer transition-colors"
              :class="selectedDbLead?.email===lead.email ? 'bg-gray-800' : 'hover:bg-gray-800/40'">
              <div class="flex items-center justify-between mb-1">
                <span class="font-semibold text-sm truncate" x-text="lead.profile?.company_name || lead.email"></span>
                <span class="text-xs px-1.5 py-0.5 rounded-full shrink-0 ml-1" :class="statusBadge(lead.status)" x-text="lead.status?.replace('_',' ')"></span>
              </div>
              <div class="text-xs text-gray-500 truncate mb-1.5" x-text="lead.email"></div>
              <div class="flex gap-1">
                <span class="text-xs px-1 py-0.5 bg-blue-900/40 text-blue-300 rounded" x-text="'AI '+(lead.profile?.ai_maturity_score??'?')"></span>
                <span class="text-xs px-1 py-0.5 bg-gray-700 text-gray-300 rounded" x-text="lead.turns+' turns'"></span>
              </div>
            </div>
          </template>
          <div x-show="filteredDb.length===0" class="p-8 text-center text-gray-600 text-sm">No live leads in database</div>
        </div>
      </template>

      <!-- TRACES LIST -->
      <template x-if="activeTab==='traces'">
        <div>
          <template x-for="tr in filteredTraces" :key="tr.trace_id">
            <div @click="selectedTrace=tr" class="p-3 border-b border-gray-800 cursor-pointer transition-colors"
              :class="selectedTrace?.trace_id===tr.trace_id ? 'bg-gray-800' : 'hover:bg-gray-800/40'">
              <div class="flex items-center justify-between mb-1">
                <span class="font-mono text-xs text-gray-300 truncate" x-text="tr.trace_id"></span>
                <span class="text-xs px-1.5 py-0.5 rounded-full shrink-0 ml-1"
                  :class="tr.reward===1 ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'"
                  x-text="tr.reward===1 ? 'Pass' : 'Fail'"></span>
              </div>
              <div class="flex gap-2 text-xs text-gray-500 mt-1">
                <span x-text="tr.segment"></span>
                <span x-text="tr.conversation_turns+' turns'"></span>
                <span x-text="tr.duration_s+'s'"></span>
              </div>
            </div>
          </template>
        </div>
      </template>

      <!-- RUN PIPELINE LEFT PANEL -->
      <template x-if="activeTab==='run'">
        <div class="flex flex-col h-full">
          <!-- Upload + config section -->
          <div class="p-3 border-b border-gray-800 space-y-2 shrink-0">
            <label class="block cursor-pointer bg-gray-800 border-2 border-dashed border-gray-600 hover:border-green-500 rounded-lg p-3 text-center transition-colors">
              <input type="file" accept=".csv" class="hidden" @change="uploadCSV($event)">
              <div class="text-sm font-medium text-gray-300">Upload CSV</div>
              <div class="text-xs text-gray-500 mt-0.5">Needs an <span class="font-mono text-green-400">email</span> column</div>
            </label>
            <div class="flex items-center gap-2">
              <span class="text-xs text-gray-400 shrink-0">Run</span>
              <input type="number" x-model.number="csvLimit" min="1" max="100"
                class="w-16 bg-gray-800 border border-gray-700 text-gray-100 text-sm px-2 py-1 rounded-lg text-center focus:outline-none focus:border-green-500">
              <span class="text-xs text-gray-400 shrink-0">companies</span>
            </div>
            <button @click="startPipeline()"
              :disabled="!csvRows.length || runJob?.status==='running'"
              class="w-full bg-green-600 hover:bg-green-500 disabled:opacity-40 disabled:cursor-not-allowed text-white text-sm font-semibold py-2 rounded-lg transition-colors">
              <span x-show="runJob?.status==='running'" class="flex items-center justify-center gap-2">
                <span class="w-3 h-3 border-2 border-white/40 border-t-white rounded-full animate-spin"></span>
                Running…
              </span>
              <span x-show="runJob?.status!=='running'">▶ Run Pipeline</span>
            </button>
            <div x-show="csvRows.length" class="text-xs text-gray-500 text-center"
              x-text="csvRows.length + ' rows loaded · running ' + Math.min(csvLimit, csvRows.length)"></div>
          </div>
          <!-- Company list -->
          <div class="flex-1 overflow-y-auto">
            <template x-for="(row, i) in runCompanies" :key="row.email||i">
              <div @click="selectedRunRow=row" class="p-3 border-b border-gray-800 cursor-pointer transition-colors"
                :class="selectedRunRow?.email===row.email ? 'bg-gray-800' : 'hover:bg-gray-800/40'">
                <div class="flex items-center justify-between mb-1">
                  <span class="font-semibold text-sm truncate"
                    x-text="row.result?.company_name||row.company||row.email"></span>
                  <span class="text-xs px-1.5 py-0.5 rounded-full shrink-0 ml-1"
                    :class="runStatusBadge(row.status)" x-text="row.status"></span>
                </div>
                <div class="text-xs text-gray-500 truncate" x-text="row.email"></div>
                <template x-if="row.status==='running'">
                  <div class="text-xs text-blue-400 mt-1 animate-pulse truncate"
                    x-text="'→ '+(row.current_step||'Running…')"></div>
                </template>
                <template x-if="row.status==='done' && row.result">
                  <div class="flex gap-1 mt-1 flex-wrap">
                    <span class="text-xs px-1 py-0.5 bg-blue-900/40 text-blue-300 rounded"
                      x-text="'AI '+row.result.ai_maturity_score"></span>
                    <span class="text-xs px-1 py-0.5 bg-purple-900/40 text-purple-300 rounded capitalize"
                      x-text="row.result.segment"></span>
                    <span x-show="row.result.email_sent"
                      class="text-xs px-1 py-0.5 bg-green-900/40 text-green-300 rounded">Email ✓</span>
                    <span x-show="row.result.booking_success"
                      class="text-xs px-1 py-0.5 bg-yellow-900/40 text-yellow-300 rounded">Booked ✓</span>
                  </div>
                </template>
                <template x-if="row.status==='error'">
                  <div class="text-xs text-red-400 mt-1 truncate" x-text="row.error"></div>
                </template>
              </div>
            </template>
            <div x-show="!runCompanies.length" class="p-8 text-center text-gray-600 text-sm">
              Upload a CSV to get started
            </div>
          </div>
        </div>
      </template>

    </div>
  </div>

  <!-- RIGHT DETAIL -->
  <div class="flex-1 overflow-y-auto bg-gray-950">

    <!-- ── PIPELINE DETAIL ── -->
    <template x-if="activeTab==='pipeline' && selectedLead">
      <div class="p-5 space-y-4" x-data="{open:'overview'}">

        <!-- Company Header Card -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <div class="flex items-start justify-between flex-wrap gap-3">
            <div>
              <h2 class="text-xl font-bold" x-text="selectedLead.input?.name || selectedLead.enrichment?.company_name"></h2>
              <a :href="'https://'+selectedLead.input?.domain" target="_blank"
                class="text-blue-400 text-sm hover:underline" x-text="selectedLead.input?.domain"></a>
              <div class="text-xs text-gray-500 mt-0.5" x-text="'Contact: '+(selectedLead.input?.contact_email||'—')"></div>
            </div>
            <div class="flex flex-col items-end gap-1.5">
              <span class="px-2.5 py-1 rounded-full text-xs font-semibold" :class="statusBadge(selectedLead._status)" x-text="selectedLead._status?.replace('_',' ')"></span>
              <span class="text-xs text-gray-600" x-text="selectedLead.processed_at||''"></span>
            </div>
          </div>

          <!-- Quick Stats -->
          <div class="mt-4 grid grid-cols-2 sm:grid-cols-5 gap-3">
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-blue-400" x-text="selectedLead.enrichment?.headcount||'N/A'"></div>
              <div class="text-xs text-gray-500 mt-0.5">Headcount</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-purple-400 text-sm" x-text="selectedLead.enrichment?.funding_stage||'N/A'"></div>
              <div class="text-xs text-gray-500 mt-0.5">Funding</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold" :class="aiColor(selectedLead.ai_maturity?.score)" x-text="(selectedLead.ai_maturity?.score??'?')+'/3'"></div>
              <div class="text-xs text-gray-500 mt-0.5">AI Maturity</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-cyan-400" x-text="selectedLead.conversation?.reply_turns?.length||0"></div>
              <div class="text-xs text-gray-500 mt-0.5">Conv. Turns</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold" :class="selectedLead.input?.country_code ? 'text-gray-200' : 'text-gray-600'" x-text="selectedLead.input?.country_code||'—'"></div>
              <div class="text-xs text-gray-500 mt-0.5">Country</div>
            </div>
          </div>

          <!-- Pipeline Progress -->
          <div class="mt-4">
            <div class="text-xs text-gray-500 mb-2">Pipeline Stages</div>
            <div class="flex items-center">
              <template x-for="(st, i) in pipelineStages(selectedLead)" :key="st.name">
                <div class="flex items-center">
                  <div class="flex flex-col items-center">
                    <div class="w-7 h-7 rounded-full flex items-center justify-center text-xs font-bold"
                      :class="st.done ? 'bg-green-500 text-black' : 'bg-gray-800 text-gray-500'">
                      <span x-show="st.done">✓</span>
                      <span x-show="!st.done" x-text="i+1"></span>
                    </div>
                    <div class="text-xs mt-1 w-14 text-center leading-tight"
                      :class="st.done ? 'text-green-400' : 'text-gray-600'" x-text="st.name"></div>
                  </div>
                  <div x-show="i<5" class="w-6 h-px mb-5" :class="st.done ? 'bg-green-500' : 'bg-gray-700'"></div>
                </div>
              </template>
            </div>
          </div>
        </div>

        <!-- ENRICHMENT -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <button @click="open=open==='enrich'?'':'enrich'" class="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-800/40">
            <div class="flex items-center gap-3">
              <span class="font-semibold text-green-400">Enrichment</span>
              <span class="text-xs text-gray-500">Multi-source company signals</span>
            </div>
            <svg class="w-4 h-4 text-gray-500 transition-transform" :class="open==='enrich'?'rotate-180':''" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div x-show="open==='enrich'" x-collapse class="px-5 pb-5 space-y-4">
            <div class="grid grid-cols-2 gap-3">
              <div class="bg-gray-800 rounded-lg p-4">
                <div class="text-xs text-gray-500 mb-1">AI Maturity Score</div>
                <div class="flex items-center gap-3">
                  <div class="text-3xl font-black" :class="aiColor(selectedLead.ai_maturity?.score)" x-text="selectedLead.ai_maturity?.score??'?'"></div>
                  <div>
                    <div class="font-semibold text-sm" :class="aiColor(selectedLead.ai_maturity?.score)" x-text="selectedLead.ai_maturity?.label"></div>
                    <div class="text-xs text-gray-500 leading-tight" x-text="selectedLead.ai_maturity?.reason"></div>
                  </div>
                </div>
              </div>
              <div class="bg-gray-800 rounded-lg p-4">
                <div class="text-xs text-gray-500 mb-1">Segment</div>
                <div class="font-bold capitalize" x-text="selectedLead.segment?.label||'generic'"></div>
                <div class="text-xs text-gray-500 mt-1 space-x-1">
                  <span x-show="selectedLead.enrichment?.recently_funded" class="text-yellow-400">Recently Funded</span>
                  <span x-show="selectedLead.enrichment?.had_layoffs" class="text-red-400">Had Layoffs</span>
                  <span x-text="(selectedLead.enrichment?.headcount_growth_pct||0)+'% growth'"></span>
                </div>
                <div class="text-xs text-gray-500 mt-1" x-text="(selectedLead.enrichment?.open_engineering_roles||0)+' open eng roles'"></div>
              </div>
            </div>

            <div class="space-y-2">
              <div class="text-xs text-gray-500 uppercase tracking-wider font-medium">Signal Sources</div>
              <template x-for="sig in enrichSignals(selectedLead)" :key="sig.name">
                <div class="bg-gray-800 rounded-lg p-3">
                  <div class="flex items-center justify-between mb-1.5">
                    <div class="flex items-center gap-2">
                      <span class="text-sm" x-text="sig.name"></span>
                      <span class="text-xs px-1.5 py-0.5 rounded font-mono"
                        :class="sig.conf>0.5 ? 'bg-green-900/50 text-green-400' : 'bg-gray-700 text-gray-400'"
                        x-text="sig.source"></span>
                    </div>
                    <span class="text-xs font-mono text-gray-400" x-text="Math.round(sig.conf*100)+'%'"></span>
                  </div>
                  <div class="h-1.5 bg-gray-700 rounded-full">
                    <div class="h-full rounded-full bar" :class="sig.conf>0.5?'bg-green-500':'bg-gray-500'" :style="'width:'+Math.round(sig.conf*100)+'%'"></div>
                  </div>
                </div>
              </template>
            </div>

            <!-- Leadership change -->
            <div x-show="selectedLead.enrichment?.leadership_change?.detected" class="bg-yellow-950/30 border border-yellow-800/30 rounded-lg p-3">
              <div class="text-xs text-yellow-400 font-medium mb-1">Leadership Change Detected</div>
              <div class="text-sm text-gray-300">
                <span x-text="selectedLead.enrichment?.leadership_change?.changed_role"></span>
                · <span x-text="selectedLead.enrichment?.leadership_change?.change_type"></span>
                · <span x-text="selectedLead.enrichment?.leadership_change?.days_since_change+' days ago'"></span>
              </div>
            </div>
          </div>
        </div>

        <!-- EMAIL -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <button @click="open=open==='email'?'':'email'" class="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-800/40">
            <div class="flex items-center gap-3">
              <span class="font-semibold text-blue-400">Email Outreach</span>
              <span class="text-xs px-2 py-0.5 rounded-full"
                :class="selectedLead.email?.tone_check ? 'bg-green-900/50 text-green-300' : 'bg-red-900/50 text-red-300'"
                x-text="selectedLead.email?.tone_check ? '✓ Tone OK' : '✗ Tone Failed'"></span>
              <span x-show="selectedLead.email?.dry_send" class="text-xs text-gray-500">dry-run</span>
            </div>
            <svg class="w-4 h-4 text-gray-500 transition-transform" :class="open==='email'?'rotate-180':''" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div x-show="open==='email'" x-collapse class="px-5 pb-5 space-y-3">
            <div class="text-xs text-gray-500">
              To: <span class="text-gray-300 font-medium" x-text="selectedLead.email?.send_result?.to || selectedLead.input?.contact_email"></span>
            </div>
            <div class="bg-gray-800 rounded-lg p-4">
              <div class="text-xs text-gray-500 mb-1">Subject</div>
              <div class="font-semibold" x-text="selectedLead.email?.subject"></div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4">
              <div class="text-xs text-gray-500 mb-2">Body</div>
              <div class="text-sm text-gray-200 whitespace-pre-wrap font-mono leading-relaxed" x-text="selectedLead.email?.body"></div>
            </div>
            <div class="bg-gray-800 rounded-lg p-3">
              <div class="text-xs text-gray-500 mb-2">Tone Check</div>
              <div class="flex gap-4 text-sm">
                <div class="flex items-center gap-1.5">
                  <div class="w-2 h-2 rounded-full" :class="selectedLead.email?.det_ok ? 'bg-green-500' : 'bg-red-500'"></div>
                  Deterministic
                </div>
                <div class="flex items-center gap-1.5">
                  <div class="w-2 h-2 rounded-full" :class="selectedLead.email?.tone_check ? 'bg-green-500' : 'bg-red-500'"></div>
                  LLM Tone
                </div>
              </div>
              <div x-show="selectedLead.email?.violations?.length" class="mt-2 space-y-1">
                <template x-for="v in selectedLead.email?.violations||[]" :key="v">
                  <div class="text-xs text-red-400 font-mono" x-text="'· '+v"></div>
                </template>
              </div>
              <div x-show="!selectedLead.email?.violations?.length" class="text-xs text-green-400 mt-2">No violations</div>
            </div>
          </div>
        </div>

        <!-- COMPETITORS -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <button @click="open=open==='comp'?'':'comp'" class="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-800/40">
            <div class="flex items-center gap-3">
              <span class="font-semibold text-orange-400">Competitor Analysis</span>
              <span class="text-xs text-gray-500" x-text="(selectedLead.competitor_gap_brief?.competitors?.length||0)+' competitors'"></span>
            </div>
            <svg class="w-4 h-4 text-gray-500 transition-transform" :class="open==='comp'?'rotate-180':''" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div x-show="open==='comp'" x-collapse class="px-5 pb-5 space-y-3">
            <div class="bg-orange-950/20 border border-orange-800/30 rounded-lg p-3">
              <div class="text-xs text-orange-400 font-medium mb-1">Recommended Angle</div>
              <div class="text-sm" x-text="selectedLead.competitor_gap_brief?.recommended_angle"></div>
            </div>
            <div class="text-xs text-gray-500" x-text="selectedLead.competitor_gap_brief?.top_gap_summary"></div>
            <template x-for="c in selectedLead.competitor_gap_brief?.competitors||[]" :key="c.name">
              <div class="bg-gray-800 rounded-lg p-4">
                <div class="font-bold mb-2" x-text="c.name"></div>
                <div class="space-y-1.5 text-sm">
                  <div><span class="text-gray-500 text-xs">Positioning: </span><span class="text-gray-300" x-text="c.positioning"></span></div>
                  <div><span class="text-red-400 text-xs">Gap: </span><span class="text-gray-300" x-text="c.gap"></span></div>
                  <div><span class="text-green-400 text-xs">Our Advantage: </span><span class="text-gray-300" x-text="c.tenacious_advantage"></span></div>
                </div>
              </div>
            </template>
          </div>
        </div>

        <!-- CONVERSATION -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <button @click="open=open==='conv'?'':'conv'" class="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-800/40">
            <div class="flex items-center gap-3">
              <span class="font-semibold text-cyan-400">Conversation</span>
              <span class="text-xs text-gray-500" x-text="(selectedLead.conversation?.reply_turns?.length||0)+' turns'"></span>
              <span x-show="selectedLead.conversation?.qualified" class="text-xs px-2 py-0.5 rounded-full bg-green-900/50 text-green-300">Qualified</span>
            </div>
            <svg class="w-4 h-4 text-gray-500 transition-transform" :class="open==='conv'?'rotate-180':''" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div x-show="open==='conv'" x-collapse class="px-5 pb-5 space-y-3">
            <!-- Initial outreach -->
            <div class="bubble-agent rounded-lg p-4">
              <div class="text-xs text-green-400 font-medium mb-1">Tenacious Agent · Initial Outreach</div>
              <div class="font-semibold text-sm mb-1" x-text="selectedLead.email?.subject"></div>
              <div class="text-sm text-gray-300 whitespace-pre-wrap" x-text="selectedLead.email?.body"></div>
            </div>
            <!-- Turns -->
            <template x-for="(turn, i) in selectedLead.conversation?.reply_turns||[]" :key="i">
              <div>
                <div class="bubble-prospect rounded-lg p-4">
                  <div class="flex items-center justify-between mb-1">
                    <span class="text-xs text-blue-400 font-medium">
                      <span x-text="selectedLead.input?.name||'Prospect'"></span> · Turn <span x-text="turn.turn"></span>
                    </span>
                    <span x-show="turn.qualified" class="text-xs bg-green-900/50 text-green-300 px-2 py-0.5 rounded-full">Qualified</span>
                  </div>
                  <div class="text-sm text-gray-200" x-text="turn.text"></div>
                </div>
              </div>
            </template>
            <div x-show="selectedLead.conversation?.qualified"
              class="bg-green-950/20 border border-green-800/30 rounded-lg p-3 text-center text-green-400 text-sm">
              Lead qualified after <span class="font-bold" x-text="selectedLead.conversation?.reply_turns?.length"></span> turns
            </div>
          </div>
        </div>

        <!-- BOOKING -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <button @click="open=open==='book'?'':'book'" class="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-800/40">
            <div class="flex items-center gap-3">
              <span class="font-semibold text-yellow-400">Booking</span>
              <span class="text-xs px-2 py-0.5 rounded-full"
                :class="selectedLead.booking?.success ? 'bg-green-900/50 text-green-300' : 'bg-red-900/50 text-red-400'"
                x-text="selectedLead.booking?.success ? '✓ Booked' : '✗ Not Booked'"></span>
            </div>
            <svg class="w-4 h-4 text-gray-500 transition-transform" :class="open==='book'?'rotate-180':''" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div x-show="open==='book'" x-collapse class="px-5 pb-5 space-y-3">
            <div class="flex justify-end">
              <button @click.stop="syncBooking(selectedLead._slug)"
                :disabled="syncState[selectedLead._slug]?.booking === 'loading'"
                class="bg-yellow-500 hover:bg-yellow-400 disabled:opacity-50 disabled:cursor-not-allowed text-black text-xs font-semibold px-4 py-1.5 rounded-lg transition-colors">
                <span x-text="syncState[selectedLead._slug]?.booking==='loading' ? 'Booking…' : syncState[selectedLead._slug]?.booking==='ok' ? '✓ Booked' : 'Book Call'"></span>
              </button>
            </div>
            <template x-if="selectedLead.booking?.success">
              <div class="bg-green-950/20 border border-green-800/30 rounded-lg p-4 space-y-2 text-sm">
                <div><span class="text-gray-500">Slot: </span><span class="font-medium" x-text="selectedLead.booking?.slot"></span></div>
                <div><span class="text-gray-500">URL: </span><a :href="selectedLead.booking?.booking_url" target="_blank" class="text-blue-400 hover:underline break-all" x-text="selectedLead.booking?.booking_url"></a></div>
              </div>
            </template>
            <template x-if="!selectedLead.booking?.success">
              <div class="bg-red-950/20 border border-red-800/30 rounded-lg p-4 text-sm">
                <span class="text-red-400">Error: </span><span x-text="selectedLead.booking?.error||'Booking not attempted'"></span>
              </div>
            </template>
          </div>
        </div>

        <!-- HUBSPOT -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <button @click="open=open==='hs'?'':'hs'" class="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-800/40">
            <div class="flex items-center gap-3">
              <span class="font-semibold text-orange-400">HubSpot CRM</span>
              <span class="text-xs px-2 py-0.5 rounded-full"
                :class="selectedLead.hubspot?.contact_id ? 'bg-green-900/50 text-green-300' : 'bg-gray-700 text-gray-400'"
                x-text="selectedLead.hubspot?.contact_id ? 'Synced' : 'Not Synced'"></span>
            </div>
            <svg class="w-4 h-4 text-gray-500 transition-transform" :class="open==='hs'?'rotate-180':''" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div x-show="open==='hs'" x-collapse class="px-5 pb-5 space-y-3">
            <div class="flex justify-end">
              <button @click.stop="syncCRM(selectedLead._slug)"
                :disabled="syncState[selectedLead._slug]?.crm === 'loading'"
                class="bg-orange-500 hover:bg-orange-400 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-semibold px-4 py-1.5 rounded-lg transition-colors">
                <span x-text="syncState[selectedLead._slug]?.crm==='loading' ? 'Syncing…' : syncState[selectedLead._slug]?.crm==='ok' ? '✓ Synced' : 'Sync to CRM'"></span>
              </button>
            </div>
            <div class="bg-gray-800 rounded-lg p-4">
              <div class="text-xs text-gray-500 mb-1">Contact ID</div>
              <div class="font-mono text-sm" x-text="selectedLead.hubspot?.contact_id||'Not synced'"></div>
            </div>
            <div class="bg-gray-800 rounded-lg p-4">
              <div class="text-xs text-gray-500 mb-2">Custom Fields</div>
              <div class="space-y-2">
                <template x-for="[k,v] in Object.entries(selectedLead.hubspot?.fields||{})" :key="k">
                  <div class="flex justify-between text-xs">
                    <span class="font-mono text-gray-500" x-text="k"></span>
                    <span class="text-gray-300" x-text="v||'—'"></span>
                  </div>
                </template>
              </div>
            </div>
          </div>
        </div>

        <!-- ABLATION -->
        <div x-show="selectedLead.ablation_comparison" class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
          <button @click="open=open==='abl'?'':'abl'" class="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-800/40">
            <div class="flex items-center gap-3">
              <span class="font-semibold text-pink-400">Ablation Comparison</span>
              <span class="text-xs text-gray-500">vs baselines</span>
            </div>
            <svg class="w-4 h-4 text-gray-500 transition-transform" :class="open==='abl'?'rotate-180':''" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/></svg>
          </button>
          <div x-show="open==='abl'" x-collapse class="px-5 pb-5 space-y-3">
            <div class="grid grid-cols-3 gap-3">
              <div class="bg-gray-800 rounded-lg p-3 text-center">
                <div class="text-xl font-bold text-pink-400" x-text="Math.round((selectedLead.ablation_comparison?.ablation_method_pass_at_1||0)*100)+'%'"></div>
                <div class="text-xs text-gray-500 mt-0.5">Our Method</div>
              </div>
              <div class="bg-gray-800 rounded-lg p-3 text-center">
                <div class="text-xl font-bold text-gray-400" x-text="Math.round((selectedLead.ablation_comparison?.tau2_bench_baseline_pass_at_1||0)*100)+'%'"></div>
                <div class="text-xs text-gray-500 mt-0.5">τ² Bench</div>
              </div>
              <div class="bg-gray-800 rounded-lg p-3 text-center">
                <div class="text-xl font-bold text-red-400" x-text="Math.round((selectedLead.ablation_comparison?.ablation_baseline_pass_at_1||0)*100)+'%'"></div>
                <div class="text-xs text-gray-500 mt-0.5">Baseline</div>
              </div>
            </div>
            <div class="text-xs text-gray-400 bg-gray-800 rounded-lg p-3" x-text="selectedLead.ablation_comparison?.note"></div>
          </div>
        </div>

      </div>
    </template>

    <!-- ── LIVE LEAD DETAIL ── -->
    <template x-if="activeTab==='live' && selectedDbLead">
      <div class="p-5 space-y-4">
        <div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <h2 class="text-xl font-bold" x-text="selectedDbLead.profile?.company_name||selectedDbLead.email"></h2>
          <div class="text-gray-500 text-sm" x-text="selectedDbLead.email"></div>
          <div x-show="selectedDbLead.phone" class="text-gray-500 text-xs mt-0.5" x-text="'Phone: '+selectedDbLead.phone"></div>
          <div class="mt-4 grid grid-cols-2 sm:grid-cols-4 gap-3">
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-sm" :class="statusBadge(selectedDbLead.status).split(' ')[1]" x-text="selectedDbLead.status?.replace('_',' ')"></div>
              <div class="text-xs text-gray-500 mt-0.5">Status</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-green-400" x-text="selectedDbLead.turns"></div>
              <div class="text-xs text-gray-500 mt-0.5">Turns</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold" :class="aiColor(selectedDbLead.profile?.ai_maturity_score)" x-text="selectedDbLead.profile?.ai_maturity_score??'N/A'"></div>
              <div class="text-xs text-gray-500 mt-0.5">AI Maturity</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold" :class="selectedDbLead.booking_url ? 'text-yellow-400' : 'text-gray-600'" x-text="selectedDbLead.booking_url ? '✓ Booked' : 'Not Booked'"></div>
              <div class="text-xs text-gray-500 mt-0.5">Booking</div>
            </div>
          </div>
          <div x-show="selectedDbLead.booking_url" class="mt-3 text-xs">
            <span class="text-gray-500">Booking URL: </span>
            <a :href="selectedDbLead.booking_url" target="_blank" class="text-blue-400 hover:underline" x-text="selectedDbLead.booking_url"></a>
          </div>
          <!-- Sync Actions -->
          <div class="flex gap-2 mt-4">
            <button @click="syncBookingDb(selectedDbLead.email)"
              :disabled="syncState['db:'+selectedDbLead.email]?.booking==='loading'"
              class="bg-yellow-500 hover:bg-yellow-400 disabled:opacity-50 disabled:cursor-not-allowed text-black text-xs font-semibold px-4 py-2 rounded-lg transition-colors">
              <span x-text="syncState['db:'+selectedDbLead.email]?.booking==='loading' ? 'Booking…' : syncState['db:'+selectedDbLead.email]?.booking==='ok' ? '✓ Booked' : 'Book Call'"></span>
            </button>
            <button @click="syncCrmDb(selectedDbLead.email)"
              :disabled="syncState['db:'+selectedDbLead.email]?.crm==='loading'"
              class="bg-orange-500 hover:bg-orange-400 disabled:opacity-50 disabled:cursor-not-allowed text-white text-xs font-semibold px-4 py-2 rounded-lg transition-colors">
              <span x-text="syncState['db:'+selectedDbLead.email]?.crm==='loading' ? 'Syncing…' : syncState['db:'+selectedDbLead.email]?.crm==='ok' ? '✓ Synced' : 'Sync to CRM'"></span>
            </button>
          </div>
        </div>

        <!-- Enrichment Profile -->
        <div x-show="selectedDbLead.profile?.company_name" class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <h3 class="font-semibold text-green-400 mb-3">Company Profile</h3>
          <div class="grid grid-cols-2 gap-2 text-sm">
            <template x-for="[k,v] in Object.entries(selectedDbLead.profile||{}).filter(([k])=>!['raw','crunchbase_signal','job_posts_signal','layoffs_signal','leadership_change_signal','leadership_change'].includes(k))" :key="k">
              <div class="flex gap-2">
                <span class="text-gray-500 text-xs capitalize shrink-0" x-text="k.replace(/_/g,' ')"></span>
                <span class="text-gray-300 text-xs font-mono break-all" x-text="JSON.stringify(v)"></span>
              </div>
            </template>
          </div>
        </div>

        <!-- Conversation history -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <h3 class="font-semibold text-cyan-400 mb-3">Conversation History</h3>
          <div class="space-y-3">
            <template x-for="(msg, i) in selectedDbLead.history||[]" :key="i">
              <div class="rounded-lg p-4" :class="msg.role==='agent' ? 'bubble-agent' : 'bubble-prospect'">
                <div class="text-xs font-medium mb-1" :class="msg.role==='agent' ? 'text-green-400' : 'text-blue-400'"
                  x-text="msg.role==='agent' ? 'Tenacious Agent' : 'Prospect'"></div>
                <div class="text-sm" x-text="msg.content"></div>
                <div class="text-xs text-gray-600 mt-1" x-text="msg.ts||''"></div>
              </div>
            </template>
            <div x-show="!selectedDbLead.history?.length" class="text-gray-600 text-sm text-center py-4">No conversation history yet</div>
          </div>
        </div>
      </div>
    </template>

    <!-- ── TRACE DETAIL ── -->
    <template x-if="activeTab==='traces' && selectedTrace">
      <div class="p-5 space-y-4">
        <div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <div class="flex items-center justify-between mb-4 flex-wrap gap-2">
            <span class="font-mono text-sm text-gray-300" x-text="selectedTrace.trace_id"></span>
            <span class="px-3 py-1 rounded-full text-sm font-semibold"
              :class="selectedTrace.reward===1 ? 'bg-green-900 text-green-300' : 'bg-red-900 text-red-300'"
              x-text="selectedTrace.reward===1 ? 'PASS' : 'FAIL'"></span>
          </div>
          <div class="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-4">
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-blue-400 capitalize" x-text="selectedTrace.segment"></div>
              <div class="text-xs text-gray-500 mt-0.5">Segment</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-cyan-400" x-text="selectedTrace.conversation_turns"></div>
              <div class="text-xs text-gray-500 mt-0.5">Turns</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-purple-400" x-text="selectedTrace.duration_s+'s'"></div>
              <div class="text-xs text-gray-500 mt-0.5">Duration</div>
            </div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center">
              <div class="font-bold text-yellow-400" x-text="'$'+selectedTrace.agent_cost_usd"></div>
              <div class="text-xs text-gray-500 mt-0.5">Cost</div>
            </div>
          </div>
          <div class="grid grid-cols-3 gap-3 mb-4">
            <div class="bg-gray-800 rounded-lg p-2.5 text-center text-sm font-medium"
              :class="selectedTrace.engaged ? 'text-green-400' : 'text-gray-600'"
              x-text="selectedTrace.engaged ? '✓ Engaged' : '✗ Not Engaged'"></div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center text-sm font-medium"
              :class="selectedTrace.qualified ? 'text-green-400' : 'text-gray-600'"
              x-text="selectedTrace.qualified ? '✓ Qualified' : '✗ Not Qualified'"></div>
            <div class="bg-gray-800 rounded-lg p-2.5 text-center text-sm font-medium"
              :class="selectedTrace.booked ? 'text-green-400' : 'text-gray-600'"
              x-text="selectedTrace.booked ? '✓ Booked' : '✗ Not Booked'"></div>
          </div>
          <div class="text-xs text-gray-500"><span class="text-gray-400">AI Maturity: </span><span x-text="selectedTrace.ai_maturity_score"></span></div>
          <div class="text-xs text-gray-500 mt-1"><span class="text-gray-400">Termination: </span><span x-text="selectedTrace.termination_reason"></span></div>
          <div class="text-xs text-gray-500 mt-1"><span class="text-gray-400">Variant: </span><span x-text="selectedTrace.outbound_variant"></span></div>
        </div>

        <div x-show="selectedTrace.enrichment_confidence" class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <h3 class="font-semibold text-green-400 mb-3">Enrichment Confidence</h3>
          <div class="space-y-2.5">
            <template x-for="[src, conf] in Object.entries(selectedTrace.enrichment_confidence||{})" :key="src">
              <div>
                <div class="flex justify-between text-xs mb-1">
                  <span class="capitalize" x-text="src"></span>
                  <span x-text="Math.round(conf*100)+'%'"></span>
                </div>
                <div class="h-1.5 bg-gray-800 rounded-full">
                  <div class="h-full rounded-full bg-green-500 bar" :style="'width:'+Math.round(conf*100)+'%'"></div>
                </div>
              </div>
            </template>
          </div>
        </div>

        <div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <h3 class="font-semibold text-blue-400 mb-3">Email</h3>
          <div class="text-sm mb-2"><span class="text-gray-500">Subject: </span><span x-text="selectedTrace.email_subject"></span></div>
          <div class="flex gap-4 text-sm">
            <span :class="selectedTrace.tone_check_passed ? 'text-green-400' : 'text-red-400'"
              x-text="selectedTrace.tone_check_passed ? '✓ Tone Passed' : '✗ Tone Failed'"></span>
            <span class="text-gray-500" x-text="selectedTrace.tone_check_retries+' retries'"></span>
          </div>
        </div>
      </div>
    </template>

    <!-- ── RUN PIPELINE: job overview (no row selected) ── -->
    <template x-if="activeTab==='run' && !selectedRunRow">
      <div class="p-5 space-y-4">

        <!-- Progress card — shown while/after job runs -->
        <template x-if="runJob">
          <div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
            <div class="flex items-center justify-between mb-4">
              <h2 class="font-bold text-lg">Pipeline Progress</h2>
              <span class="text-xs px-2.5 py-1 rounded-full font-semibold"
                :class="runJob.status==='done' ? 'bg-green-900 text-green-300' : 'bg-blue-900 text-blue-300 animate-pulse'"
                x-text="runJob.status==='done' ? '✓ Complete' : '⏳ Running…'"></span>
            </div>
            <div class="grid grid-cols-4 gap-3 mb-4">
              <div class="bg-gray-800 rounded-lg p-3 text-center">
                <div class="text-2xl font-bold text-white" x-text="runJob.companies.length"></div>
                <div class="text-xs text-gray-500 mt-0.5">Total</div>
              </div>
              <div class="bg-gray-800 rounded-lg p-3 text-center">
                <div class="text-2xl font-bold text-blue-400"
                  x-text="runJob.companies.filter(c=>c.status==='running').length"></div>
                <div class="text-xs text-gray-500 mt-0.5">Running</div>
              </div>
              <div class="bg-gray-800 rounded-lg p-3 text-center">
                <div class="text-2xl font-bold text-green-400"
                  x-text="runJob.companies.filter(c=>c.status==='done').length"></div>
                <div class="text-xs text-gray-500 mt-0.5">Done</div>
              </div>
              <div class="bg-gray-800 rounded-lg p-3 text-center">
                <div class="text-2xl font-bold text-red-400"
                  x-text="runJob.companies.filter(c=>c.status==='error').length"></div>
                <div class="text-xs text-gray-500 mt-0.5">Failed</div>
              </div>
            </div>
            <!-- Progress bar -->
            <div>
              <div class="flex justify-between text-xs text-gray-500 mb-1">
                <span>Progress</span>
                <span x-text="Math.round(runJob.companies.filter(c=>['done','error'].includes(c.status)).length / runJob.companies.length * 100) + '%'"></span>
              </div>
              <div class="h-2.5 bg-gray-800 rounded-full overflow-hidden">
                <div class="h-full bg-green-500 bar rounded-full"
                  :style="'width:' + Math.round(runJob.companies.filter(c=>['done','error'].includes(c.status)).length / runJob.companies.length * 100) + '%'"></div>
              </div>
            </div>
            <!-- Pipeline stage summary when done -->
            <template x-if="runJob.status==='done'">
              <div class="mt-4 pt-4 border-t border-gray-800 grid grid-cols-3 gap-3 text-center text-sm">
                <div>
                  <div class="font-bold text-blue-400"
                    x-text="runJob.companies.filter(c=>c.result?.email_sent).length"></div>
                  <div class="text-xs text-gray-500">Emails Sent</div>
                </div>
                <div>
                  <div class="font-bold text-orange-400"
                    x-text="runJob.companies.filter(c=>c.result?.hubspot_contact_id).length"></div>
                  <div class="text-xs text-gray-500">CRM Synced</div>
                </div>
                <div>
                  <div class="font-bold text-purple-400"
                    x-text="runJob.companies.filter(c=>c.result).length ? (runJob.companies.filter(c=>c.result).reduce((s,c)=>s+(c.result.ai_maturity_score||0),0)/runJob.companies.filter(c=>c.result).length).toFixed(1) : '—'"></div>
                  <div class="text-xs text-gray-500">Avg AI Score</div>
                </div>
              </div>
            </template>
          </div>
        </template>

        <!-- CSV format guide -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <h3 class="font-semibold text-green-400 mb-3">CSV Format Guide</h3>
          <div class="text-sm text-gray-400 space-y-3">
            <p>Upload a CSV with at least an <span class="font-mono text-green-300">email</span> column.
               The domain is extracted from the email for enrichment.</p>
            <div class="bg-gray-800 rounded-lg p-3 font-mono text-xs text-gray-300 leading-relaxed">
              email,company_name,contact_name<br>
              cto@stripe.com,Stripe,Patrick C.<br>
              eng@notion.so,Notion,<br>
              founder@seed.vc,,
            </div>
            <div class="grid grid-cols-2 gap-2 text-xs text-gray-500">
              <div><span class="text-gray-300 font-mono">email</span> — required</div>
              <div><span class="text-gray-300 font-mono">company_name</span> — display name</div>
              <div><span class="text-gray-300 font-mono">contact_name</span> — person name</div>
              <div><span class="text-gray-300 font-mono">domain</span> — fallback if no email</div>
            </div>
          </div>
          <div class="mt-4 pt-4 border-t border-gray-800">
            <div class="text-xs text-gray-500 font-medium mb-2">Pipeline stages per company</div>
            <div class="flex items-center gap-1 flex-wrap">
              <template x-for="(st, i) in ['Enrich','Signals','Segment','Comp. Gap','Email Compose','Email Send','Booking','CRM Sync']" :key="st">
                <div class="flex items-center gap-1">
                  <span class="text-xs bg-gray-800 text-gray-400 px-2 py-0.5 rounded" x-text="st"></span>
                  <span x-show="i<7" class="text-gray-700 text-xs">→</span>
                </div>
              </template>
            </div>
          </div>
        </div>

      </div>
    </template>

    <!-- ── RUN PIPELINE: selected row detail ── -->
    <template x-if="activeTab==='run' && selectedRunRow">
      <div class="p-5 space-y-4" x-data="{openStep: null}">

        <!-- Company header -->
        <div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
          <div class="flex items-center justify-between mb-2 flex-wrap gap-2">
            <div>
              <h2 class="text-xl font-bold"
                x-text="selectedRunRow.result?.company_name||selectedRunRow.company||selectedRunRow.email"></h2>
              <div class="text-sm text-gray-500 mt-0.5" x-text="selectedRunRow.email"></div>
            </div>
            <span class="text-xs px-2.5 py-1 rounded-full font-semibold"
              :class="runStatusBadge(selectedRunRow.status)" x-text="selectedRunRow.status"></span>
          </div>

          <!-- Live step indicator -->
          <template x-if="selectedRunRow.status==='running'">
            <div class="mt-3 flex items-center gap-2 text-sm text-blue-400">
              <span class="w-3 h-3 border-2 border-blue-400/40 border-t-blue-400 rounded-full animate-spin shrink-0"></span>
              <span x-text="selectedRunRow.current_step ? 'Running: '+selectedRunRow.current_step : 'Pipeline running…'"></span>
            </div>
          </template>
          <template x-if="selectedRunRow.status==='pending'">
            <div class="mt-3 text-sm text-gray-500">⏸ Waiting in queue…</div>
          </template>
          <template x-if="selectedRunRow.status==='error'">
            <div class="mt-3 bg-red-950/30 border border-red-800/30 rounded-lg p-3 text-sm text-red-400"
              x-text="selectedRunRow.error"></div>
          </template>

          <!-- Summary badges when done -->
          <template x-if="selectedRunRow.status==='done' && selectedRunRow.result">
            <div class="mt-4 grid grid-cols-4 gap-2">
              <div class="bg-gray-800 rounded-lg p-2.5 text-center">
                <div class="font-bold" :class="aiColor(selectedRunRow.result.ai_maturity_score)"
                  x-text="selectedRunRow.result.ai_maturity_score+'/3'"></div>
                <div class="text-xs text-gray-500 mt-0.5">AI Score</div>
              </div>
              <div class="bg-gray-800 rounded-lg p-2.5 text-center">
                <div class="font-bold text-purple-400 capitalize text-sm"
                  x-text="selectedRunRow.result.segment"></div>
                <div class="text-xs text-gray-500 mt-0.5">Segment</div>
              </div>
              <div class="bg-gray-800 rounded-lg p-2.5 text-center">
                <div class="font-bold text-sm"
                  :class="selectedRunRow.result.email_sent ? 'text-green-400' : 'text-red-400'"
                  x-text="selectedRunRow.result.email_sent ? '✓ Sent' : '✗ Not Sent'"></div>
                <div class="text-xs text-gray-500 mt-0.5">Email</div>
              </div>
              <div class="bg-gray-800 rounded-lg p-2.5 text-center">
                <div class="font-bold text-sm"
                  :class="selectedRunRow.result.booking_success ? 'text-yellow-400' : 'text-gray-600'"
                  x-text="selectedRunRow.result.booking_success ? '✓ Booked' : '✗ Not Booked'"></div>
                <div class="text-xs text-gray-500 mt-0.5">Booking</div>
              </div>
            </div>
          </template>
        </div>

        <!-- 8-step accordion — rendered from steps array, works live too -->
        <template x-for="(st, si) in (selectedRunRow.steps||[])" :key="st.step">
          <div class="bg-gray-900 rounded-xl border overflow-hidden"
            :class="st.status==='error' ? 'border-red-800/50' : 'border-gray-800'">
            <button @click="openStep = openStep===si ? null : si"
              class="w-full px-5 py-3.5 flex items-center justify-between hover:bg-gray-800/40 text-left">
              <div class="flex items-center gap-3">
                <span class="text-sm font-mono text-gray-400" x-text="(si+1)+'.'"></span>
                <span class="font-semibold"
                  :class="{
                    'text-green-400':  st.step==='Enrichment',
                    'text-teal-400':   st.step==='Signals Research',
                    'text-purple-400': st.step==='Segment',
                    'text-orange-400': st.step==='Competitor Gap',
                    'text-blue-400':   st.step==='Email Compose',
                    'text-cyan-400':   st.step==='Email Send',
                    'text-yellow-400': st.step==='Booking',
                    'text-pink-400':   st.step==='CRM Sync',
                  }"
                  x-text="st.step"></span>
                <span class="text-xs px-2 py-0.5 rounded-full font-medium"
                  :class="st.status==='done' ? 'bg-green-900/50 text-green-300' : st.status==='error' ? 'bg-red-900/50 text-red-300' : 'bg-gray-700 text-gray-400'"
                  x-text="st.status==='done' ? '✓ done' : st.status==='error' ? '✗ error' : '…'"></span>
                <span x-show="st.error" class="text-xs text-red-400 truncate max-w-xs" x-text="st.error"></span>
              </div>
              <svg class="w-4 h-4 text-gray-500 shrink-0 transition-transform"
                :class="openStep===si ? 'rotate-180' : ''"
                fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7"/>
              </svg>
            </button>

            <div x-show="openStep===si" class="px-5 pb-5 space-y-3">

              <!-- ENRICHMENT detail -->
              <template x-if="st.step==='Enrichment'">
                <div class="space-y-3">
                  <div class="grid grid-cols-2 gap-2 text-sm">
                    <div class="bg-gray-800 rounded-lg p-3">
                      <div class="text-xs text-gray-500 mb-1">Company</div>
                      <div class="font-semibold" x-text="st.output.company_name||'—'"></div>
                      <div class="text-xs text-gray-400 mt-0.5" x-text="st.output.domain||''"></div>
                    </div>
                    <div class="bg-gray-800 rounded-lg p-3">
                      <div class="text-xs text-gray-500 mb-1">AI Maturity</div>
                      <div class="font-bold text-xl" :class="aiColor(st.output.ai_maturity_score)"
                        x-text="(st.output.ai_maturity_score??'?')+'/3'"></div>
                    </div>
                    <div class="bg-gray-800 rounded-lg p-3">
                      <div class="text-xs text-gray-500 mb-1">Headcount</div>
                      <div class="font-semibold text-gray-200" x-text="st.output.headcount||'N/A'"></div>
                    </div>
                    <div class="bg-gray-800 rounded-lg p-3">
                      <div class="text-xs text-gray-500 mb-1">Funding</div>
                      <div class="font-semibold text-yellow-400" x-text="st.output.funding_stage||'N/A'"></div>
                    </div>
                  </div>
                  <div class="space-y-2">
                    <div class="text-xs text-gray-500 uppercase tracking-wider">Signals</div>
                    <template x-for="[key, sig] in [['crunchbase_signal',st.output.crunchbase_signal],['job_posts_signal',st.output.job_posts_signal],['layoffs_signal',st.output.layoffs_signal],['leadership_change_signal',st.output.leadership_change_signal]]" :key="key">
                      <div x-show="sig" class="bg-gray-800 rounded-lg p-3">
                        <div class="flex justify-between mb-1.5">
                          <span class="text-sm capitalize" x-text="key.replace('_signal','').replace(/_/g,' ')"></span>
                          <span class="text-xs font-mono text-gray-400" x-text="Math.round((sig?.confidence||0)*100)+'%'"></span>
                        </div>
                        <div class="h-1.5 bg-gray-700 rounded-full">
                          <div class="h-full rounded-full bar"
                            :class="(sig?.confidence||0)>0.5 ? 'bg-green-500' : 'bg-gray-500'"
                            :style="'width:'+Math.round((sig?.confidence||0)*100)+'%'"></div>
                        </div>
                        <div class="text-xs text-gray-500 mt-1" x-text="sig?.source||''"></div>
                      </div>
                    </template>
                  </div>
                  <div x-show="st.output.leadership_change?.detected"
                    class="bg-yellow-950/30 border border-yellow-800/30 rounded-lg p-3">
                    <div class="text-xs text-yellow-400 font-medium mb-1">Leadership Change</div>
                    <div class="text-sm text-gray-300">
                      <span x-text="st.output.leadership_change?.changed_role"></span>
                      · <span x-text="st.output.leadership_change?.change_type"></span>
                      · <span x-text="(st.output.leadership_change?.days_since_change||'?')+' days ago'"></span>
                    </div>
                  </div>
                </div>
              </template>

              <!-- SIGNALS RESEARCH detail -->
              <template x-if="st.step==='Signals Research'">
                <div class="space-y-3">
                  <div x-show="st.output.error" class="bg-yellow-950/30 border border-yellow-800/30 rounded-lg p-3 text-xs text-yellow-400"
                    x-text="'Scraper note: '+st.output.error"></div>
                  <div x-show="st.output.tagline" class="bg-gray-800 rounded-lg p-3">
                    <div class="text-xs text-gray-500 mb-1">Tagline</div>
                    <div class="text-sm text-gray-200" x-text="st.output.tagline"></div>
                  </div>
                  <div x-show="st.output.recent_post" class="bg-gray-800 rounded-lg p-3">
                    <div class="text-xs text-gray-500 mb-1">Recent Post</div>
                    <div class="text-sm text-gray-200 italic" x-text="'&quot;'+st.output.recent_post+'&quot;'"></div>
                  </div>
                  <div x-show="st.output.product_hint" class="bg-gray-800 rounded-lg p-3">
                    <div class="text-xs text-gray-500 mb-1">Product Hint</div>
                    <div class="text-sm text-gray-200" x-text="st.output.product_hint"></div>
                  </div>
                  <div x-show="st.output.tech_hints?.length" class="bg-gray-800 rounded-lg p-3">
                    <div class="text-xs text-gray-500 mb-2">Tech Stack Detected</div>
                    <div class="flex flex-wrap gap-1.5">
                      <template x-for="t in st.output.tech_hints||[]" :key="t">
                        <span class="text-xs bg-teal-900/40 text-teal-300 px-2 py-0.5 rounded" x-text="t"></span>
                      </template>
                    </div>
                  </div>
                  <div x-show="st.output.personalization_hook" class="bg-teal-950/20 border border-teal-800/30 rounded-lg p-3">
                    <div class="text-xs text-teal-400 font-medium mb-1">Personalization Hook</div>
                    <div class="text-sm text-gray-200" x-text="st.output.personalization_hook"></div>
                  </div>
                  <div x-show="st.output.source_urls?.length" class="text-xs text-gray-600">
                    <span class="text-gray-500">Scraped: </span>
                    <template x-for="u in st.output.source_urls||[]" :key="u">
                      <a :href="u" target="_blank" class="text-blue-500 hover:underline mr-2 break-all" x-text="u"></a>
                    </template>
                  </div>
                </div>
              </template>

              <!-- SEGMENT detail -->
              <template x-if="st.step==='Segment'">
                <div class="space-y-3">
                  <div class="bg-gray-800 rounded-lg p-4 text-center">
                    <div class="text-2xl font-black text-purple-400 capitalize" x-text="st.output.segment_label"></div>
                    <div class="text-xs text-gray-500 mt-1" x-text="'Segment ID: '+st.output.segment_id"></div>
                  </div>
                  <div class="grid grid-cols-2 gap-2 text-sm">
                    <div class="bg-gray-800 rounded-lg p-3 flex items-center gap-2">
                      <div class="w-2 h-2 rounded-full shrink-0"
                        :class="st.output.recently_funded ? 'bg-yellow-400' : 'bg-gray-600'"></div>
                      <span class="text-xs text-gray-400">Recently Funded</span>
                    </div>
                    <div class="bg-gray-800 rounded-lg p-3 flex items-center gap-2">
                      <div class="w-2 h-2 rounded-full shrink-0"
                        :class="st.output.had_layoffs ? 'bg-red-400' : 'bg-gray-600'"></div>
                      <span class="text-xs text-gray-400">Had Layoffs (120d)</span>
                    </div>
                    <div class="bg-gray-800 rounded-lg p-3 flex items-center gap-2">
                      <div class="w-2 h-2 rounded-full shrink-0"
                        :class="st.output.leadership_change_detected ? 'bg-yellow-400' : 'bg-gray-600'"></div>
                      <span class="text-xs text-gray-400">Leadership Change</span>
                    </div>
                    <div class="bg-gray-800 rounded-lg p-3 flex items-center gap-2">
                      <div class="w-2 h-2 rounded-full shrink-0"
                        :class="aiColor(st.output.ai_maturity_score).replace('text-','bg-')"></div>
                      <span class="text-xs text-gray-400" x-text="'AI Maturity: '+st.output.ai_maturity_score+'/3'"></span>
                    </div>
                  </div>
                  <div x-show="st.output.open_engineering_roles" class="text-xs text-gray-500">
                    <span x-text="st.output.open_engineering_roles+' open engineering roles'"></span>
                  </div>
                </div>
              </template>

              <!-- COMPETITOR GAP detail -->
              <template x-if="st.step==='Competitor Gap'">
                <div class="space-y-3">
                  <div x-show="st.output.recommended_angle"
                    class="bg-orange-950/20 border border-orange-800/30 rounded-lg p-3">
                    <div class="text-xs text-orange-400 font-medium mb-1">Recommended Angle</div>
                    <div class="text-sm text-gray-200" x-text="st.output.recommended_angle"></div>
                  </div>
                  <div x-show="st.output.top_gap_summary" class="text-xs text-gray-400"
                    x-text="st.output.top_gap_summary"></div>
                  <template x-for="c in st.output.competitors||[]" :key="c.name">
                    <div class="bg-gray-800 rounded-lg p-4 space-y-1.5">
                      <div class="font-bold" x-text="c.name"></div>
                      <div class="text-xs"><span class="text-gray-500">Positioning: </span><span class="text-gray-300" x-text="c.positioning"></span></div>
                      <div class="text-xs"><span class="text-red-400">Gap: </span><span class="text-gray-300" x-text="c.gap"></span></div>
                      <div class="text-xs"><span class="text-green-400">Our Advantage: </span><span class="text-gray-300" x-text="c.tenacious_advantage"></span></div>
                    </div>
                  </template>
                </div>
              </template>

              <!-- EMAIL COMPOSE detail -->
              <template x-if="st.step==='Email Compose'">
                <div class="space-y-3">
                  <div class="bg-gray-800 rounded-lg p-4">
                    <div class="text-xs text-gray-500 mb-1">Subject</div>
                    <div class="font-semibold" x-text="st.output.subject||'—'"></div>
                  </div>
                  <div class="bg-gray-800 rounded-lg p-4">
                    <div class="text-xs text-gray-500 mb-2">Body</div>
                    <div class="text-sm text-gray-200 whitespace-pre-wrap font-mono leading-relaxed"
                      x-text="st.output.body||'—'"></div>
                  </div>
                  <div class="bg-gray-800 rounded-lg p-3">
                    <div class="text-xs text-gray-500 mb-2">Tone Checks</div>
                    <div class="flex gap-4 text-sm">
                      <div class="flex items-center gap-1.5">
                        <div class="w-2 h-2 rounded-full" :class="st.output.det_ok ? 'bg-green-500' : 'bg-red-500'"></div>
                        Deterministic
                      </div>
                      <div class="flex items-center gap-1.5">
                        <div class="w-2 h-2 rounded-full" :class="st.output.tone_check ? 'bg-green-500' : 'bg-red-500'"></div>
                        LLM Tone
                      </div>
                      <div class="flex items-center gap-1.5 text-gray-400 text-xs">
                        Confidence: <span class="font-mono ml-1" x-text="st.output.signal_confidence_avg"></span>
                      </div>
                    </div>
                    <div x-show="st.output.violations?.length" class="mt-2 space-y-1">
                      <template x-for="v in st.output.violations||[]" :key="v">
                        <div class="text-xs text-red-400 font-mono" x-text="'· '+v"></div>
                      </template>
                    </div>
                  </div>
                </div>
              </template>

              <!-- EMAIL SEND detail -->
              <template x-if="st.step==='Email Send'">
                <div class="space-y-3">
                  <div class="grid grid-cols-2 gap-2">
                    <div class="bg-gray-800 rounded-lg p-3 text-center">
                      <div class="font-bold text-sm"
                        :class="st.output.sent ? 'text-green-400' : 'text-red-400'"
                        x-text="st.output.sent ? '✓ Sent' : '✗ Failed'"></div>
                      <div class="text-xs text-gray-500 mt-0.5">Status</div>
                    </div>
                    <div class="bg-gray-800 rounded-lg p-3 text-center">
                      <div class="font-bold text-xs"
                        :class="st.output.outbound_live ? 'text-green-400' : 'text-yellow-400'"
                        x-text="st.output.outbound_live ? 'LIVE' : 'SINK (dev)'"></div>
                      <div class="text-xs text-gray-500 mt-0.5">Mode</div>
                    </div>
                  </div>
                  <div class="bg-gray-800 rounded-lg p-3 text-sm space-y-1.5">
                    <div><span class="text-gray-500">To: </span><span class="font-mono text-gray-300" x-text="st.output.to"></span></div>
                    <div><span class="text-gray-500">Routed to: </span><span class="font-mono text-gray-300" x-text="st.output.routed_to"></span></div>
                    <div x-show="st.output.resend_id"><span class="text-gray-500">Resend ID: </span><span class="font-mono text-gray-400 text-xs" x-text="st.output.resend_id"></span></div>
                  </div>
                  <div x-show="st.output.error"
                    class="bg-red-950/30 border border-red-800/30 rounded-lg p-3 text-xs text-red-400"
                    x-text="st.output.error"></div>
                </div>
              </template>

              <!-- BOOKING detail -->
              <template x-if="st.step==='Booking'">
                <div class="space-y-3">
                  <div class="bg-gray-800 rounded-lg p-4 text-center">
                    <div class="font-bold text-lg"
                      :class="st.output.success ? 'text-yellow-400' : 'text-gray-600'"
                      x-text="st.output.success ? '✓ Booked' : '✗ Not Booked'"></div>
                  </div>
                  <template x-if="st.output.success">
                    <div class="bg-green-950/20 border border-green-800/30 rounded-lg p-4 space-y-2 text-sm">
                      <div><span class="text-gray-500">Slot: </span><span class="font-medium" x-text="st.output.slot"></span></div>
                      <div><span class="text-gray-500">URL: </span>
                        <a :href="st.output.booking_url" target="_blank"
                          class="text-blue-400 hover:underline break-all" x-text="st.output.booking_url"></a>
                      </div>
                    </div>
                  </template>
                  <div x-show="st.output.error"
                    class="bg-red-950/30 border border-red-800/30 rounded-lg p-3 text-sm text-red-400"
                    x-text="st.output.error"></div>
                </div>
              </template>

              <!-- CRM SYNC detail -->
              <template x-if="st.step==='CRM Sync'">
                <div class="space-y-3">
                  <div class="bg-gray-800 rounded-lg p-4">
                    <div class="text-xs text-gray-500 mb-1">HubSpot Contact ID</div>
                    <div class="font-mono text-sm"
                      :class="st.output.contact_id ? 'text-green-400' : 'text-gray-500'"
                      x-text="st.output.contact_id||'Not synced'"></div>
                  </div>
                  <div x-show="st.output.error"
                    class="bg-red-950/30 border border-red-800/30 rounded-lg p-2 text-xs text-red-400"
                    x-text="'CRM error: '+st.output.error"></div>
                  <div x-show="st.output.fields" class="bg-gray-800 rounded-lg p-4">
                    <div class="text-xs text-gray-500 mb-2">Fields Written to HubSpot</div>
                    <div class="space-y-1.5">
                      <template x-for="[k,v] in Object.entries(st.output.fields||{})" :key="k">
                        <div class="flex gap-2 text-xs">
                          <span class="font-mono text-gray-500 w-36 shrink-0" x-text="k"></span>
                          <span class="text-gray-300 break-all" x-text="v||'—'"></span>
                        </div>
                      </template>
                    </div>
                  </div>
                </div>
              </template>

            </div>
          </div>
        </template>

        <!-- Empty state while pending / in-flight with no steps yet -->
        <template x-if="!(selectedRunRow.steps||[]).length && selectedRunRow.status!=='error'">
          <div class="text-center text-gray-600 py-8 text-sm">
            <span x-text="selectedRunRow.status==='pending' ? '⏸ Waiting in queue…' : 'Pipeline steps will appear here as they complete.'"></span>
          </div>
        </template>

      </div>
    </template>

    <!-- Empty state for non-run tabs -->
    <template x-if="(activeTab==='pipeline'&&!selectedLead)||(activeTab==='live'&&!selectedDbLead)||(activeTab==='traces'&&!selectedTrace)">
      <div class="flex items-center justify-center h-full text-gray-700">
        <div class="text-center">
          <div class="text-6xl mb-3">←</div>
          <div>Select a lead to view details</div>
        </div>
      </div>
    </template>
  </div>
</div>

<script>
function app() {
  return {
    loading: false,
    toast: null,
    syncState: {},
    lastUpdated: 'Never',
    activeTab: 'pipeline',
    search: '',
    filterStatus: '',
    filterSegment: '',
    batchLeads: [],
    dbLeads: [],
    traces: [],
    selectedLead: null,
    selectedDbLead: null,
    selectedTrace: null,
    stats: [],
    // Run Pipeline state
    csvRows: [],
    csvLimit: 5,
    runJobId: null,
    runJob: null,
    runPolling: null,
    selectedRunRow: null,
    tabs: [
      { id: 'pipeline', label: 'Pipeline Results', count: 0 },
      { id: 'live',     label: 'Live Leads',       count: 0 },
      { id: 'traces',   label: 'Eval Traces',      count: 0 },
      { id: 'run',      label: '▶ Run Pipeline',   count: 0 },
    ],

    async init() {
      await this.refresh();
      setInterval(() => this.refresh(), 30000);
    },

    async refresh() {
      this.loading = true;
      try {
        const [br, dr, tr] = await Promise.all([
          fetch('/api/batch-results').then(r => r.json()),
          fetch('/api/db-leads').then(r => r.json()),
          fetch('/api/traces').then(r => r.json()),
        ]);
        this.batchLeads = (br || []).map(l => ({
          ...l,
          _status: l.booking?.success       ? 'booked'
                 : l.conversation?.qualified ? 'qualified'
                 : (l.conversation?.reply_turns?.length||0) > 0 ? 'in_conversation'
                 : l.email                  ? 'outreach_sent'
                 :                            'new'
        }));
        this.dbLeads = Array.isArray(dr) ? dr : [];
        this.traces  = tr || [];
        this.tabs[0].count = this.batchLeads.length;
        this.tabs[1].count = this.dbLeads.length;
        this.tabs[2].count = this.traces.length;
        this.computeStats();
        this.lastUpdated = 'Updated ' + new Date().toLocaleTimeString();
      } catch(e) { console.error(e); }
      this.loading = false;
    },

    computeStats() {
      const ls = this.batchLeads;
      const emails    = ls.filter(l => l.email).length;
      const conv      = ls.filter(l => (l.conversation?.reply_turns?.length||0) > 0).length;
      const qualified = ls.filter(l => l.conversation?.qualified).length;
      const booked    = ls.filter(l => l.booking?.success).length;
      const avgAI     = ls.length ? (ls.reduce((s,l)=>s+(l.ai_maturity?.score||0),0)/ls.length).toFixed(1) : '0';
      const toneOk    = ls.filter(l => l.email?.tone_check).length;
      this.stats = [
        { label:'Total',         value: ls.length,              color:'text-white' },
        { label:'Emails Sent',   value: emails,                 color:'text-blue-400' },
        { label:'In Conv.',      value: conv,                   color:'text-cyan-400' },
        { label:'Qualified',     value: qualified,              color:'text-green-400' },
        { label:'Booked',        value: booked,                 color:'text-yellow-400' },
        { label:'Avg AI Score',  value: avgAI,                  color:'text-purple-400' },
        { label:'Tone Pass',     value: toneOk+'/'+emails,      color:'text-pink-400' },
      ];
    },

    get filteredBatch() {
      return this.batchLeads.filter(l => {
        const name  = (l.input?.name || l.enrichment?.company_name || '').toLowerCase();
        const email = (l.input?.contact_email || '').toLowerCase();
        const q = this.search.toLowerCase();
        return (!q || name.includes(q) || email.includes(q))
            && (!this.filterStatus  || l._status === this.filterStatus)
            && (!this.filterSegment || l.segment?.label === this.filterSegment);
      });
    },

    get filteredDb() {
      return this.dbLeads.filter(l => {
        const name = (l.profile?.company_name || l.email || '').toLowerCase();
        const q = this.search.toLowerCase();
        return (!q || name.includes(q) || l.email.includes(q))
            && (!this.filterStatus || l.status === this.filterStatus);
      });
    },

    get filteredTraces() {
      return this.traces.filter(t => {
        const q = this.search.toLowerCase();
        return (!q || t.trace_id.includes(q) || (t.segment||'').includes(q))
            && (!this.filterSegment || t.segment === this.filterSegment);
      });
    },

    statusBadge(s) {
      return { new:'bg-gray-700 text-gray-300', outreach_sent:'bg-blue-900/60 text-blue-300',
               in_conversation:'bg-cyan-900/60 text-cyan-300', qualified:'bg-green-900/60 text-green-300',
               disqualified:'bg-red-900/60 text-red-300', booked:'bg-yellow-900/60 text-yellow-300' }[s]
             || 'bg-gray-700 text-gray-300';
    },

    aiColor(s) {
      return s>=3 ? 'text-green-400' : s>=2 ? 'text-yellow-400' : s>=1 ? 'text-orange-400' : 'text-red-400';
    },

    enrichSignals(lead) {
      if (!lead?.enrichment) return [];
      const e = lead.enrichment;
      return [
        { name:'Crunchbase',         conf: e.crunchbase_signal?.confidence||0,         source: e.crunchbase_signal?.source||'?' },
        { name:'Job Posts',          conf: e.job_posts_signal?.confidence||0,          source: e.job_posts_signal?.source||'?' },
        { name:'Layoffs.fyi',        conf: e.layoffs_signal?.confidence||0,            source: e.layoffs_signal?.source||'?' },
        { name:'Leadership (PDL)',   conf: e.leadership_change_signal?.confidence||0,  source: e.leadership_change_signal?.source||'?' },
      ];
    },

    pipelineStages(lead) {
      return [
        { name:'Enrich',   done: !!lead.enrichment },
        { name:'Email',    done: !!lead.email },
        { name:'Converse', done: (lead.conversation?.reply_turns?.length||0)>0 },
        { name:'Qualify',  done: !!lead.conversation?.qualified },
        { name:'Book',     done: !!lead.booking?.success },
        { name:'CRM',      done: !!lead.hubspot?.contact_id },
      ];
    },

    showToast(type, msg) {
      this.toast = { type, message: msg };
      setTimeout(() => this.toast = null, 4500);
    },

    async syncBooking(slug) {
      if (!this.syncState[slug]) this.syncState[slug] = {};
      this.syncState[slug].booking = 'loading';
      try {
        const r = await fetch('/api/sync-booking/' + slug, { method: 'POST' }).then(x => x.json());
        if (r.success) {
          this.selectedLead.booking = r;
          this.selectedLead._status = 'booked';
          const idx = this.batchLeads.findIndex(l => l._slug === slug);
          if (idx >= 0) { this.batchLeads[idx].booking = r; this.batchLeads[idx]._status = 'booked'; }
          this.showToast('success', 'Booked: ' + (r.slot || r.booking_url));
          this.syncState[slug].booking = 'ok';
        } else {
          this.showToast('error', 'Booking failed: ' + (r.error || 'unknown error'));
          this.syncState[slug].booking = 'error';
        }
      } catch(e) {
        this.showToast('error', 'Request failed: ' + e.message);
        this.syncState[slug].booking = 'error';
      }
    },

    async syncCRM(slug) {
      if (!this.syncState[slug]) this.syncState[slug] = {};
      this.syncState[slug].crm = 'loading';
      try {
        const r = await fetch('/api/sync-crm/' + slug, { method: 'POST' }).then(x => x.json());
        if (r.success) {
          if (this.selectedLead.hubspot) this.selectedLead.hubspot.contact_id = r.contact_id;
          else this.selectedLead.hubspot = { contact_id: r.contact_id, fields: {} };
          this.showToast('success', 'CRM synced · contact #' + r.contact_id);
          this.syncState[slug].crm = 'ok';
        } else {
          this.showToast('error', 'CRM sync failed: ' + (r.error || 'unknown error'));
          this.syncState[slug].crm = 'error';
        }
      } catch(e) {
        this.showToast('error', 'Request failed: ' + e.message);
        this.syncState[slug].crm = 'error';
      }
    },

    async syncBookingDb(email) {
      const key = 'db:' + email;
      if (!this.syncState[key]) this.syncState[key] = {};
      this.syncState[key].booking = 'loading';
      try {
        const r = await fetch('/api/sync-booking-db', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email}) }).then(x => x.json());
        if (r.success) {
          this.selectedDbLead.booking_url = r.booking_url;
          this.showToast('success', 'Booked: ' + (r.slot || r.booking_url));
          this.syncState[key].booking = 'ok';
        } else {
          this.showToast('error', 'Booking failed: ' + (r.error || 'unknown error'));
          this.syncState[key].booking = 'error';
        }
      } catch(e) {
        this.showToast('error', 'Request failed: ' + e.message);
        this.syncState[key].booking = 'error';
      }
    },

    async syncCrmDb(email) {
      const key = 'db:' + email;
      if (!this.syncState[key]) this.syncState[key] = {};
      this.syncState[key].crm = 'loading';
      try {
        const r = await fetch('/api/sync-crm-db', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({email}) }).then(x => x.json());
        if (r.success) {
          this.showToast('success', 'CRM synced · contact #' + r.contact_id);
          this.syncState[key].crm = 'ok';
        } else {
          this.showToast('error', 'CRM sync failed: ' + (r.error || 'unknown error'));
          this.syncState[key].crm = 'error';
        }
      } catch(e) {
        this.showToast('error', 'Request failed: ' + e.message);
        this.syncState[key].crm = 'error';
      }
    },

    // ── Run Pipeline ────────────────────────────────────────────────────────
    async uploadCSV(e) {
      const file = e.target.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append('file', file);
      try {
        const rows = await fetch('/api/upload-csv', { method:'POST', body:fd }).then(r => r.json());
        this.csvRows = Array.isArray(rows) ? rows : [];
        this.runJob = null;
        this.runJobId = null;
        this.selectedRunRow = null;
        this.tabs[3].count = this.csvRows.length;
        this.showToast('success', `Loaded ${this.csvRows.length} companies from CSV`);
      } catch(err) {
        this.showToast('error', 'CSV upload failed: ' + err.message);
      }
    },

    async startPipeline() {
      if (!this.csvRows.length) { this.showToast('error', 'Upload a CSV first'); return; }
      if (this.runJob?.status === 'running') { this.showToast('error', 'Pipeline already running'); return; }
      try {
        const r = await fetch('/api/start-pipeline', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rows: this.csvRows, limit: this.csvLimit }),
        }).then(x => x.json());
        if (!r.job_id) { this.showToast('error', 'Failed to start'); return; }
        this.runJobId = r.job_id;
        this.runJob   = null;
        this.selectedRunRow = null;
        if (this.runPolling) clearInterval(this.runPolling);
        this.runPolling = setInterval(() => this.pollPipeline(), 2000);
        this.showToast('success', `Pipeline started for ${r.count} companies`);
      } catch(err) {
        this.showToast('error', 'Start failed: ' + err.message);
      }
    },

    async pollPipeline() {
      if (!this.runJobId) return;
      try {
        const job = await fetch('/api/pipeline-status/' + this.runJobId).then(r => r.json());
        this.runJob = job;
        // keep selectedRunRow in sync with latest data
        if (this.selectedRunRow) {
          const fresh = job.companies.find(c => c.email === this.selectedRunRow.email);
          if (fresh) this.selectedRunRow = fresh;
        }
        if (job.status === 'done') {
          clearInterval(this.runPolling);
          this.runPolling = null;
          await this.refresh();
          const done = job.companies.filter(c => c.status === 'done').length;
          const errs = job.companies.filter(c => c.status === 'error').length;
          this.showToast('success', `Pipeline complete — ${done} OK, ${errs} failed`);
        }
      } catch(_e) {}
    },

    get runCompanies() {
      if (this.runJob) return this.runJob.companies;
      return this.csvRows.map(r => ({ ...r, status:'pending', result:null, error:'' }));
    },

    runStatusBadge(s) {
      return { pending:'bg-gray-700 text-gray-400', running:'bg-blue-800 text-blue-200 animate-pulse',
               done:'bg-green-900 text-green-300', error:'bg-red-900 text-red-300' }[s]
             || 'bg-gray-700 text-gray-400';
    },
  }
}
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run("dashboard:app", host="0.0.0.0", port=8001, reload=True)
