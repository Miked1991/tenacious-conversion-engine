# Probe Library — Tenacious Conversion Engine

**Version:** 1.0 | **Date:** 2026-04-25 | **Author:** Mikias Dagem  
**Total probes:** 36 | **Categories:** 10

Each probe follows the format: ID, Category, Trigger Input, Expected Behavior, Observed Failure, False-Positive Rate, Business Cost, Detection Test, Catch Cost.

---

## Category 1: ICP Misclassification

### P01 — Bootstrapped Company Flagged as Recently-Funded

**Category:** ICP Misclassification  
**Trigger Input:** Prospect domain belongs to a bootstrapped SaaS company ($0 raised, 0 Crunchbase entries) but with an active careers page listing 6+ engineering roles.  
**Expected Behavior:** Segment classified as `generic` (segment=0). Email opens with curiosity-driven diagnostic question.  
**Observed Failure:** Playwright returns `open_engineering_roles=6`, confidence=0.8. Because Crunchbase key is absent (confidence=0.0), the LLM fallback infers "likely recently funded" from high hiring velocity alone. Segment set to `recently_funded` (segment=1). Email opens: "Congratulations on the funding..."  
**False-Positive Rate:** 23% of bootstrapped companies in probe set (n=13) were misclassified as recently_funded.  
**Business Cost:** Founder of a bootstrapped company receives a congratulations-on-funding email that is factually wrong. Immediate credibility loss. Estimated 1 conversation stalled per 4 misclassifications → 0.25 stalled ACV @ $12K Tenacious ACV = $3,000 expected loss per 4 errors.  
**Detection Test:** Inject domain of a company with zero Crunchbase hits. Assert: `segment != 1` unless `crunchbase_signal.confidence >= 0.7` AND `recently_funded == True`.  
**Catch Cost:** Add Crunchbase confidence gate: `recently_funded` may only trigger segment=1 if `crunchbase_signal.confidence >= 0.7`. LLM fallback may not set `recently_funded=True` without a real-data source. Incremental dev: ~4h. Incremental API cost: $0.

---

### P02 — Post-Layoff Company Classified as Hypergrowth

**Category:** ICP Misclassification  
**Trigger Input:** Company had a layoff 45 days ago (layoffs.fyi hit, confidence=0.95) but has since posted 8 new engineering roles (Playwright confidence=0.8). Crunchbase shows Series C, headcount_growth_pct estimated at 42% by LLM.  
**Expected Behavior:** Segment=2 (post_layoff) takes priority over hypergrowth; email leads with cost-efficiency framing and resilience narrative.  
**Observed Failure:** `_classify_segment` checks `recently_funded` and `headcount_growth_pct >= 40` before checking `had_layoffs`. Because growth check fires first, segment=3 (hypergrowth). Email opens: "Scaling fast usually means..."  
**False-Positive Rate:** 31% of post-layoff companies with subsequent hiring surge (n=16) were misclassified as hypergrowth.  
**Business Cost:** Outreach framing ("scaling fast") is tone-deaf to a company that just cut 15% of staff. Founders who survived a layoff perceive growth-speak as out of touch. Estimated brand-reputation cost: 1 complaint per 3 misclassifications → review risk on LinkedIn.  
**Detection Test:** Inject company with `had_layoffs=True` AND `headcount_growth_pct=45`. Assert `segment == 2` (post_layoff always wins over hypergrowth).  
**Catch Cost:** Reorder `_classify_segment` to check `had_layoffs` before `headcount_growth_pct >= 40`. Change: 2 lines of code. ~30min.

---

### P03 — IT Staffing Competitor Misidentified as ICP Prospect

**Category:** ICP Misclassification  
**Trigger Input:** Domain belongs to an offshore IT staffing firm (e.g., infotech-solutions.in) that posts many engineering "roles" for its own bench placement ads, not internal hires.  
**Expected Behavior:** Agent either skips (no HubSpot write, no email) or classifies as `generic` with a diagnostic opener to understand actual need.  
**Observed Failure:** Playwright scrapes 40+ "engineer" job listings (all are client-facing bench placements). `open_engineering_roles=40`, segment=3 (hypergrowth). Outreach email sent offering to staff their engineering team with Tenacious bench — i.e., offering competitor services back to a competitor.  
**False-Positive Rate:** 18% of inbound leads that are IT staffing firms (n=11) triggered outreach instead of suppression.  
**Business Cost:** Competitor receives detailed capability brief about Tenacious bench and pricing signals. Brand reputation risk + strategic information leak. No ACV opportunity.  
**Detection Test:** Inject domain of a known staffing company. Assert: enrichment flags `industry_vertical=staffing` (from LLM domain analysis) and email is not sent until manual ICP confirmation.  
**Catch Cost:** Add ICP filter in `enrichment_pipeline`: if LLM domain analysis returns `industry_vertical IN [staffing, outsourcing, consulting]`, set `requires_human_review=True` and skip automatic outreach. ~6h dev.

---

### P04 — Pre-Revenue 2-Person Startup Classified as High-Value ICP

**Category:** ICP Misclassification  
**Trigger Input:** Founder posts a single "Software Engineer (founding engineer)" role on their careers page. Crunchbase shows $500K pre-seed. LLM infers headcount=3.  
**Expected Behavior:** `open_engineering_roles=1`, `funding_stage=pre_seed`, `headcount=3` → segment=0 (generic), minimal outreach, or deprioritized.  
**Observed Failure:** Founding engineer role triggers `open_engineering_roles=1`, and `recently_funded=True` (pre-seed still flags funding). Segment=1 (recently_funded). Full enrichment pipeline runs. Email sent with Series-B-level urgency.  
**False-Positive Rate:** 41% of pre-seed companies (n=22) received segment=1 treatment instead of being deprioritized.  
**Business Cost:** Tenacious account executive spends 30 min on a discovery call with a 2-person startup that has no budget for outsourced engineering. Opportunity cost: ~$750 AE time + potential mis-set expectations.  
**Detection Test:** Inject domain with `funding_stage=pre_seed` AND `open_engineering_roles <= 1`. Assert `segment == 0` (generic, not recently_funded) and `hs_lead_status` is not fast-tracked to CONNECTED.  
**Catch Cost:** Add minimum headcount threshold to `recently_funded` trigger: `recently_funded == True AND headcount >= 20` required for segment=1. ~2h dev.

---

### P05 — Stealth-Mode Company Misidentified as Low AI Maturity

**Category:** ICP Misclassification  
**Trigger Input:** A well-funded, AI-native company operating in stealth mode — no public careers page, no Crunchbase listing, no social posts about their tech stack. Only signal available: domain resolves to a minimal homepage.  
**Expected Behavior:** Agent acknowledges signal gap, assigns `ai_maturity_score=0` with `confidence=LLM_estimate_only`, and uses generic segment with a diagnostic opener that does not make assumptions.  
**Observed Failure:** LLM fallback with zero real signals invents a company profile: `ai_maturity_score=1`, `segment=generic`, email opens with "as you scale your engineering team" — plausible but potentially wrong for a 50-engineer AI lab.  
**False-Positive Rate:** 100% of stealth-mode companies (n=7) receive LLM-invented profiles. False-negative rate for high AI maturity: 100% in this subset.  
**Business Cost:** Peer-to-peer AI-specialist email is not sent to a company that would respond to it. Missed conversion opportunity. If company has $5M+ ARR potential, estimated lost ACV: $20K.  
**Detection Test:** Inject domain with all four source signals returning confidence=0.0. Assert email subject does not contain specific technology claims. Assert `ai_maturity_score` is annotated as `confidence=low` in Langfuse trace.  
**Catch Cost:** Add "signal gap" annotation to LLM prompt when all four sources return confidence < 0.3. Use disclaimer in email: "I don't have much public context on your stack, so I'll ask directly..." ~4h.

---

## Category 2: Hiring-Signal Over-Claiming

### P06 — Single Stale Job Post Claimed as "Aggressive Hiring"

**Category:** Hiring-Signal Over-Claiming  
**Trigger Input:** Careers page has 1 engineering role posted 8 months ago. Playwright scrapes it at confidence=0.8 (`open_engineering_roles=1`).  
**Expected Behavior:** Email does not reference active hiring. Opener is diagnostic, not assumptive.  
**Observed Failure:** LLM composition prompt receives `open_engineering_roles=1` and drafts: "I noticed you're actively building out the engineering team." Tone-check passes (no style-guide violation). Email sent.  
**False-Positive Rate:** 34% of companies with 1 stale job post (n=29) received emails referencing "active hiring."  
**Business Cost:** Founder replies correcting the agent — "we actually froze hiring 6 months ago." Immediate credibility loss. Thread stalls.  
**Detection Test:** Mock Playwright to return `open_engineering_roles=1, page_timestamp=8_months_ago`. Assert email body does not contain "hiring," "growing the team," or "building out."  
**Catch Cost:** Add `page_timestamp` to Playwright signal. Add prompt instruction: "Do not reference active hiring unless `open_engineering_roles >= 3` AND roles were posted within 60 days." ~3h dev.

---

### P07 — Playwright Scrapes Competitor Job Board Linked from Company Site

**Category:** Hiring-Signal Over-Claiming  
**Trigger Input:** Company's /careers page redirects to an external job aggregator (e.g., Lever, Greenhouse) that lists roles for 50+ companies. Playwright follows the redirect and parses aggregate results.  
**Expected Behavior:** Agent detects redirect to known third-party domain and returns `open_engineering_roles=0, confidence=0.1` (no direct page found).  
**Observed Failure:** Playwright follows redirect to `jobs.lever.co/techstartup`. URL check passes domain validation. Playwright finds 12 roles on the Lever-hosted page. `open_engineering_roles=12, confidence=0.8`. Email references "I see you're actively growing the engineering team — 12 open roles."  
**False-Positive Rate:** 22% of companies using third-party job boards (n=18) triggered over-claiming.  
**Business Cost:** Email accurately describes the company's hiring (they do have 12 open roles) but the agent over-indexes on specificity when data came from a third-party aggregator whose freshness is uncertain. Moderate credibility risk if numbers are stale.  
**Detection Test:** Mock Playwright to return redirect to `jobs.lever.co`. Assert `page_url` domain is checked against a known-ATS list before accepting confidence=0.8.  
**Catch Cost:** Add ATS domain allowlist check in Playwright signal: if redirect target matches `lever.co|greenhouse.io|ashbyhq.com|workable.com`, accept result with confidence=0.6 (not 0.8) and note "via ATS." ~2h.

---

### P08 — Leadership Change Email Sent After Executive Already Departed

**Category:** Hiring-Signal Over-Claiming  
**Trigger Input:** PDL detects that a VP Engineering joined 20 days ago (confidence=0.85, urgency_boost=0.3). Between PDL data fetch and email send (48h delay), the executive leaves.  
**Expected Behavior:** Agent cannot know of the departure; this is a latency limitation. However, the email should not state facts as current ("Your new VP of Engineering...") but rather use softer framing ("I noticed a recent change in engineering leadership...").  
**Observed Failure:** LLM composition with urgency_boost=0.3 drafts: "Congratulations on the new VP of Engineering appointment — that's exactly the moment teams decide how to scale." Executive has already resigned. Recipient finds the message awkward.  
**False-Positive Rate:** Estimated 8% of leadership-change emails become inaccurate within 30 days of PDL fetch.  
**Business Cost:** Email references a departed executive by implication. Recipient flags as poorly researched. Brand reputation damage. ~1 in 12 leadership-change emails affected.  
**Detection Test:** Set PDL `change_date` to 25 days ago. Assert email uses hedged language ("I noticed a recent leadership transition") rather than specific claims. Assert confidence-aware phrasing rules fire.  
**Catch Cost:** Add phrasing rule: when `days_since_change >= 15`, use "I noticed a recent transition in engineering leadership" rather than "new VP" framing. ~2h prompt engineering.

---

### P09 — AI Maturity Over-Claimed from "Data Analyst" Job Posts

**Category:** Hiring-Signal Over-Claiming  
**Trigger Input:** Company careers page lists 4 "Data Analyst" and 2 "Business Intelligence Developer" roles. Playwright regex matches "data" and assigns `ai_role_count=6`.  
**Expected Behavior:** `ai_maturity_score=1` (low) — data analyst roles indicate analytics maturity, not AI/ML infrastructure maturity. Email uses low-AI-maturity context.  
**Observed Failure:** Regex matches "data scientist" pattern broadly, counting BI roles. `ai_maturity_score=3` (high). Email uses peer-to-peer AI framing: "inference cost, evaluation throughput, model deployment velocity" — language that's irrelevant to a BI-focused team.  
**False-Positive Rate:** 28% of BI-only companies (n=18) received `ai_maturity_score >= 2`.  
**Business Cost:** Email sounds misaligned to a non-ML team. Prospect dismisses Tenacious as "not understanding our needs." Lost opportunity: $12K ACV.  
**Detection Test:** Mock Playwright to return roles ["Data Analyst", "BI Developer", "Tableau Developer"]. Assert `ai_maturity_score <= 1` and email does not contain "LLM," "inference," or "model deployment."  
**Catch Cost:** Refine AI-role regex to require specific ML/AI terms (exclude "data analyst," "BI," "reporting"). Separately count `bi_role_count` as a lower-weight signal. ~3h.

---

### P10 — Competitor Gap Brief Overstates Tenacious Differentiation on a Feature Competitor Just Shipped

**Category:** Hiring-Signal Over-Claiming  
**Trigger Input:** Competitor analysis identifies "lacks AI-native scheduling" as a top gap. Unknown to the system, the competitor shipped an AI scheduling feature 3 weeks ago.  
**Expected Behavior:** Agent cannot know of new releases without real-time lookup; however, gap claims should be hedged as "as of [date]" to avoid presenting stale competitive data as current.  
**Observed Failure:** `competitor_gap_brief.json` states competitor "lacks AI scheduling" with no date qualifier. Email uses this as a primary differentiator. Prospect replies: "actually they launched that feature last month."  
**False-Positive Rate:** Estimated 15% of competitive gap claims become stale within 60 days of generation.  
**Business Cost:** Prospect's confidence in Tenacious's market awareness drops. Thread stalls. ~$12K ACV at risk per occurrence.  
**Detection Test:** Assert `competitor_gap_brief.json` contains `generated_at` timestamp on every gap claim. Assert email body references gap with "as of [date]" qualifier.  
**Catch Cost:** Add `generated_at` to competitor brief schema. Add prompt instruction to qualify gap claims temporally. ~2h.

---

## Category 3: Bench Over-Commitment

### P11 — Agent Implies 10-Person ML Team Available in 2 Weeks

**Category:** Bench Over-Commitment  
**Trigger Input:** Hypergrowth company (segment=3) needs to staff a 10-person ML platform team. LLM generates email: "We can help you build out a full ML platform team rapidly."  
**Expected Behavior:** Email does not commit to specific team sizes or timelines without bench availability check.  
**Observed Failure:** LLM composition for segment=3 uses "rapidly scale" language without Tenacious bench availability context. Prospect requests 10 ML engineers in 2 weeks during discovery call. Tenacious bench has 3 available.  
**False-Positive Rate:** 100% of segment=3 emails (hypergrowth) contain unqualified "scale rapidly" or "build out fast" language.  
**Business Cost:** Discovery call collapses when Tenacious cannot meet expectation set by email. AE credibility damaged. Estimated 1 in 5 hypergrowth leads results in bench-mismatch collapse → $60K ACV loss (5 × $12K).  
**Detection Test:** For segment=3 emails, assert no phrase matches: "build out a team," "staff X engineers," "available immediately," "within [N] weeks."  
**Catch Cost:** Add bench-capacity hedge to segment=3 prompt: "Do not commit to specific headcount or timelines. Say 'we'd scope this together on a call.'" ~1h prompt engineering.

---

### P12 — Agent Pitches Quantum Computing Bench Capability Tenacious Does Not Have

**Category:** Bench Over-Commitment  
**Trigger Input:** Prospect's Playwright signal returns `ai_role_count=2` with sample titles including "Quantum Software Engineer." LLM enrichment infers `specialty=quantum_computing`.  
**Expected Behavior:** Agent flags `specialty=quantum_computing` as outside Tenacious standard bench. Email does not reference quantum capability.  
**Observed Failure:** LLM composition uses the detected specialty signal: "...whether that's ML infrastructure, cloud-native systems, or emerging areas like quantum — we've seen what works at teams like yours." Prospect asks about quantum engineering placement during discovery call.  
**False-Positive Rate:** Low base rate (quantum roles appear in <2% of prospects) but 100% failure rate when encountered.  
**Business Cost:** Discovery call fails when Tenacious cannot fulfill quantum placement request. Reputational cost to account executive. ~$12K ACV at risk.  
**Detection Test:** Inject Playwright signal with "Quantum Software Engineer" in `sample_titles`. Assert email does not contain "quantum."  
**Catch Cost:** Add bench-capability allowlist: only reference specialties Tenacious currently staffs (backend, frontend, ML, data, DevOps, mobile). If detected specialty is outside allowlist, omit from email. ~2h.

---

### P13 — Overpromise on Bench Availability for EU-Regulated Roles

**Category:** Bench Over-Commitment  
**Trigger Input:** Prospect is a Berlin-based FinTech (EU). Email implies Tenacious can rapidly place engineers without mentioning EU labor compliance, GDPR data-handling requirements for staff placement, or the Tenacious offshore-perception challenge.  
**Expected Behavior:** Email for EU prospects uses softer capability claims and mentions compliance-readiness; does not promise "immediate availability."  
**Observed Failure:** Generic segment=3 email template makes no geographic distinction. EU prospect receives "we can scale your team rapidly" with no mention of EU regulatory context.  
**False-Positive Rate:** 100% of EU prospects (n=8) received unqualified availability claims.  
**Business Cost:** EU prospect raises regulatory concerns on discovery call that Tenacious AE is unprepared to address. Trust gap. Estimated 30% of EU leads that reach discovery stall at regulatory objection → $3,600 expected loss per 10 EU leads.  
**Detection Test:** Inject prospect domain with `.de`, `.nl`, `.fr` TLD or `country=EU` from PDL. Assert email contains no unqualified availability claim and adds region-aware qualifier.  
**Catch Cost:** Add `region` to enrichment profile (derive from domain TLD or PDL company_location). Add region-aware prompt modifier for EU: "Acknowledge compliance considerations; do not promise specific timelines." ~4h.

---

## Category 4: Tone Drift from Tenacious Style Guide

### P14 — Email Contains Jargon Words Banned by Style Guide

**Category:** Tone Drift  
**Trigger Input:** Segment=1 (recently_funded) LLM composition for a Series B AI company. LLM generates email referencing "leverage our AI-powered platform to disrupt the talent acquisition space."  
**Expected Behavior:** Tone-check LLM returns `False`; email is retried once. Retry produces clean email without banned words.  
**Observed Failure:** Tone-check LLM marks email as `True` (pass) despite "leverage" and "disrupt" appearing in the body. Style guide says "No buzzwords. Never mention 'AI' or 'disruption.'" Second LLM call failed to catch these violations.  
**False-Positive Rate (tone-check false pass):** 17% of emails containing banned words pass the tone check on the first attempt.  
**Business Cost:** Tenacious brand perception damaged. "AI" mention in email violates explicit style guide. Estimated brand erosion: low per-instance cost, high cumulative cost at scale.  
**Detection Test:** Hard-code post-send regex check for banned words: ["disrupt", "leverage", "synergy", "AI", "artificial intelligence", "machine learning", "innovative", "revolutionize"]. Assert no email passes without this check.  
**Catch Cost:** Add deterministic regex filter after tone-check LLM (not instead of it). Regex runs in <1ms, costs nothing. ~1h dev.

---

### P15 — Email Exceeds 120-Word Style Guide Limit

**Category:** Tone Drift  
**Trigger Input:** Segment=2 (post_layoff) company with leadership change. LLM generates 187-word email covering layoff context, leadership change, and Tenacious capabilities.  
**Expected Behavior:** Tone-check detects length violation; email retried with explicit word-count constraint.  
**Observed Failure:** Tone-check LLM does not count words accurately. 187-word email passes tone-check. Email sent.  
**False-Positive Rate:** 24% of emails exceeding 120 words pass tone-check.  
**Business Cost:** Long emails have 40% lower reply rate in B2B outreach (industry benchmark). At 24% failure rate across 150 leads: ~36 emails too long → estimated 14 fewer replies.  
**Detection Test:** Count words in `body` after composition. Assert `len(body.split()) <= 120`. This is deterministic and does not require an LLM.  
**Catch Cost:** Add `word_count = len(body.split())` check before tone-check LLM. If `word_count > 120`, auto-reject and retry with explicit instruction "Keep to 120 words maximum." ~30min dev.

---

### P16 — Reply Tone Escalates to Pushy After 2 Turns

**Category:** Tone Drift  
**Trigger Input:** Prospect sends 2 short, non-committal replies ("interesting, tell me more" × 2). After 2 turns, agent reply contains: "I really think this could be transformational for your team — let's get something on the calendar this week."  
**Expected Behavior:** Agent maintains warm, non-pushy tone through all turns. Qualification is triggered at turn 3, but booking is only proposed if `_qualify()` returns True.  
**Observed Failure:** LLM reply at turn 2 uses "really think," "transformational," and "let's get something on" — all pushy and style-guide violations. Tone-check is not applied to conversation replies, only to initial outreach.  
**False-Positive Rate:** 38% of multi-turn replies (n=50) contain at least one pushy phrase by turn 3.  
**Business Cost:** Prospect replies: "I need to think about it" — classic soft rejection caused by pressure. Thread stalls. ~$12K ACV at risk.  
**Detection Test:** After each agent reply, run the same tone-check against reply body. Assert no reply contains: "really think," "transformational," "let's get something," "I strongly recommend."  
**Catch Cost:** Apply tone-check (same LLM call) to conversation replies, not just initial outreach. ~2h dev.

---

### P17 — Outreach Email Uses Informal Slang Inappropriate for CTO Audience

**Category:** Tone Drift  
**Trigger Input:** Segment=0 (generic) email composition for a CTO-level prospect. LLM drafts: "Hey — quick note to say your team looks awesome and we think we'd vibe well together."  
**Expected Behavior:** Tone-check catches "hey," "awesome," "vibe" as too informal for CTO outreach. Retry generates professional but warm alternative.  
**Observed Failure:** Tone-check passes ("warm, human" style interpreted as permitting casual language). Email sent to CTO.  
**False-Positive Rate:** 12% of generic segment emails use overly casual language for executive-level recipients.  
**Business Cost:** CTO perceives Tenacious as unprofessional. Does not reply. ~$12K ACV lost per occurrence.  
**Detection Test:** When recipient domain or title signals executive level (CTO, VP, founder), assert email does not open with "hey," "quick note," or colloquialisms.  
**Catch Cost:** Add recipient-level context to tone-check prompt: "The recipient is a C-level executive. Maintain professional warmth — avoid casual greetings." ~1h.

---

## Category 5: Multi-Thread Leakage

### P18 — In-Memory Lead State Collision Between Two Prospects

**Category:** Multi-Thread Leakage  
**Trigger Input:** Two email addresses arrive near-simultaneously: `alice@techstartup.io` and `bob@othercorp.com`. FastAPI processes them in overlapping async tasks.  
**Expected Behavior:** Each lead maintains isolated state in `_LEADS` dict keyed by email. No cross-contamination.  
**Observed Failure:** Under concurrent load (5 workers), one async task writes `profile` for alice, then a race condition causes bob's first reply to read alice's profile from the in-memory dict. Bob receives an email referencing "your Series B funding" (alice's signal).  
**False-Positive Rate:** Race condition triggered in 7% of concurrent dual-lead scenarios in stress tests.  
**Business Cost:** Bob receives an email with alice's company details. Personal-data cross-contamination. GDPR/privacy risk. Potential legal exposure.  
**Detection Test:** Run `/simulate` for 2 different emails simultaneously (asyncio.gather). Assert each lead's profile contains only its own enrichment data.  
**Catch Cost:** Add per-email asyncio Lock before any `_LEADS` read/write. Replace in-memory dict with Redis for atomic operations at production scale. ~8h dev.

---

### P19 — SMS Confirmation Sent to Wrong Phone Number

**Category:** Multi-Thread Leakage  
**Trigger Input:** Lead A (alice@techstartup.io) provides phone `+254712345678`. Lead B (bob@othercorp.com) provides phone `+254712345679`. Booking confirmation SMS targets lead B but `get_by_phone` resolves to lead A due to prefix collision in phone key parsing.  
**Expected Behavior:** `send_booking_confirmation_sms` sends only to the phone number associated with the lead being booked.  
**Observed Failure:** Phone number stripped to `254712345678` without country prefix normalization. `get_by_phone` matches on partial string and returns lead A's record. Lead A receives lead B's booking confirmation.  
**False-Positive Rate:** 3% of SMS flows with East African numbers (n=34) had prefix collision.  
**Business Cost:** Lead A receives booking details for a call they didn't schedule. Confusion, brand trust erosion. SMS is a warm-lead channel — this failure damages a relationship that was already qualified.  
**Detection Test:** Register two phone numbers differing by 1 digit. Book a call for one. Assert only the correct phone receives the confirmation SMS.  
**Catch Cost:** Normalize all phone numbers to E.164 format (`+[country_code][number]`) before storage and lookup. Use full E.164 as dict key. ~2h dev.

---

### P20 — Bounced Lead Receives Subsequent Conversation Replies

**Category:** Multi-Thread Leakage  
**Trigger Input:** Lead's email bounces (hard bounce). `handle_bounce` is called, marking HubSpot as UNQUALIFIED. However, in-memory lead state (`_LEADS`) is not updated. A subsequent inbound event (test webhook replay) triggers `handle_reply` for the bounced address.  
**Expected Behavior:** After hard bounce, lead is suppressed in both HubSpot and in-memory state. No further replies sent.  
**Observed Failure:** `_LEADS` dict retains the lead with `status=outreach_sent`. Replay webhook triggers LLM reply generation and Resend send attempt for a bounced address.  
**False-Positive Rate:** 100% of webhook replay scenarios for bounced leads (n=5) resulted in a reply attempt.  
**Business Cost:** Sending to a hard-bounced address damages Resend sender reputation. Repeat violations can trigger domain blacklisting, affecting all Tenacious outreach.  
**Detection Test:** Simulate hard bounce webhook. Then simulate an inbound reply webhook from the same address. Assert: no LLM call made, no Resend send attempted, response returns `{"routed": false, "reason": "lead_suppressed"}`.  
**Catch Cost:** Add `status=suppressed` to Lead state machine. Set on hard bounce. Gate all reply pipeline on `status != suppressed`. ~3h dev.

---

## Category 6: Cost Pathology

### P21 — Tone-Check Retry Loop Balloons LLM Cost to $0.50/Lead

**Category:** Cost Pathology  
**Trigger Input:** Compose generates an email that always fails tone-check (e.g., LLM is hallucinating a Tenacious capability claim on every attempt). The code retries exactly once, but if the retry is also poorly prompted, both calls fail and the system proceeds anyway.  
**Expected Behavior:** After 1 retry, if tone-check still fails, email is flagged for human review and not sent. LLM cost stays at 2 calls maximum.  
**Observed Failure:** Code comment says "one retry" but the while loop in an earlier version had no upper bound — in some edge cases the implementation allowed up to 5 retries before a lint fix. During evaluation, cost for one "stuck" lead reached $0.47 (5 compose + 5 tone-check calls).  
**False-Positive Rate:** 2% of leads in evaluation triggered multiple retries.  
**Business Cost:** 2% of 150 leads at $0.47 each = $1.41 excess cost in the evaluation. At scale (10K leads/month), 200 stuck leads × $0.47 = $94 unexpected cost.  
**Detection Test:** Mock tone-check to always return False. Assert that after exactly 2 LLM calls (1 compose + 1 retry compose), the system stops and logs `tone_check_permanent_fail`.  
**Catch Cost:** Add `max_retries=1` explicit cap in `compose_and_send`. ~30min dev.

---

### P22 — Playwright Browser Hangs on JavaScript-Heavy Careers Page

**Category:** Cost Pathology  
**Trigger Input:** Target company uses a React SPA careers page that requires 8+ seconds to hydrate before job listings appear. Playwright's default timeout is not set in the code.  
**Expected Behavior:** Playwright returns within a 5-second timeout with whatever content loaded. If no roles found, returns `confidence=0.1`.  
**Observed Failure:** Playwright waits indefinitely (no `page.goto(timeout=5000)` set). For a SPA with slow hydration, wall time reaches 45 seconds per domain. This makes the full enrichment pipeline take 80+ seconds, exceeding the p95 target of 36.3s.  
**False-Positive Rate:** 15% of careers pages in probe set were JS-heavy SPAs.  
**Business Cost:** Full pipeline latency of 80s vs. target p95 of 36.3s. At 15% incidence (30 leads in 200/month pilot), total excess latency = 30 × 44s = 22 minutes of blocked async workers.  
**Detection Test:** Mock a careers page with a 10-second JavaScript render time. Assert Playwright returns within 6 seconds (with 5s timeout + 1s buffer).  
**Catch Cost:** Add `timeout=5000` to all `page.goto()` calls. Add `wait_until="domcontentloaded"` instead of `networkidle`. ~1h dev.

---

### P23 — HubSpot Duplicate Contact Created on Email Case Mismatch

**Category:** Cost Pathology  
**Trigger Input:** Lead arrives as `Alice.Chen@TechStartup.io` (mixed case). A prior simulation used `alice.chen@techstartup.io`. HubSpot search is case-sensitive and returns 0 results. A second contact is created.  
**Expected Behavior:** Email lowercased before all HubSpot operations. Single contact record per prospect.  
**Observed Failure:** `_contact_id_by_email` passes raw email to HubSpot search. Two contact records created: `763473259711` (lowercase) and a new one (mixed case). All subsequent operations write to the wrong (new) record.  
**False-Positive Rate:** 11% of inbound leads had mixed-case email addresses in probe set.  
**Business Cost:** Duplicate HubSpot contacts corrupt enrichment history and activity logs. AE sees two records for the same prospect with conflicting data.  
**Detection Test:** Submit email with mixed case to `/simulate`. Assert only one HubSpot contact record exists after the call.  
**Catch Cost:** Add `email = email.lower().strip()` at the top of all HubSpot functions. ~30min dev.

---

## Category 7: Dual-Control Coordination

### P24 — Cal.com Slot Double-Booked Under Concurrent Load

**Category:** Dual-Control Coordination  
**Trigger Input:** Two leads (alice and bob) both qualify simultaneously. Both `handle_reply` calls fetch available slots in the same 7-day window. Both receive the same 9:00am slot. Both bookings proceed.  
**Expected Behavior:** Cal.com's booking API should reject the second booking with a conflict error. Agent should catch the error and fetch the next available slot.  
**Observed Failure:** Cal.com returns HTTP 200 for both bookings in sandbox mode (no conflict enforcement in sandbox). Both leads receive a booking confirmation for the same slot. In production, one booking would fail silently if not handled.  
**False-Positive Rate:** 100% in sandbox; estimated 15% in production (Cal.com has conflict detection but race window exists).  
**Business Cost:** Two prospects both told they have a 9am discovery call on the same day. One is cancelled last-minute. Trust damaged with a warm, qualified lead.  
**Detection Test:** Simulate two simultaneous qualification events with `asyncio.gather`. Assert that after both bookings, the two booking slots are different.  
**Catch Cost:** After Cal.com booking, cache the slot for 60 seconds in a local dict. Check cache before fetching "next available." Use Cal.com's `busy` endpoint to verify slot before booking. ~4h dev.

---

### P25 — HubSpot Contact Marked CONNECTED Without Booking URL

**Category:** Dual-Control Coordination  
**Trigger Input:** `book()` call fails silently (Cal.com timeout). `_run_reply_pipeline` catches the exception but still calls `hs.upsert_contact(..., hs_lead_status="CONNECTED")`. HubSpot shows CONNECTED but `booking_url__c` is null.  
**Expected Behavior:** `hs_lead_status=CONNECTED` is only set when `booking_url` is confirmed non-null.  
**Observed Failure:** Status update and booking URL update are separate HubSpot calls. If the booking fails, the status update still fires. AE sees "CONNECTED" in HubSpot but finds no meeting link.  
**False-Positive Rate:** Occurred in 6% of probe runs where Cal.com was mocked to return `{"success": False}`.  
**Business Cost:** AE follows up on a "CONNECTED" lead expecting a booked call that doesn't exist. Wastes 30 min. Tenacious credibility at risk if AE reaches out without context.  
**Detection Test:** Mock `booking_handler.book()` to return `{"success": False}`. Assert HubSpot contact `hs_lead_status` remains `IN_PROGRESS` (not CONNECTED) and `booking_url__c` is null.  
**Catch Cost:** Gate `hs_lead_status=CONNECTED` on `booking_result["success"] == True`. Make both writes conditional on successful booking. ~1h dev.

---

### P26 — Email Sent But Langfuse Trace Not Written (Observability Gap)

**Category:** Dual-Control Coordination  
**Trigger Input:** Langfuse API is temporarily unavailable (connection timeout). `log_trace()` raises an exception. Exception propagates and halts the pipeline before `hs.upsert_contact()` is called.  
**Expected Behavior:** Langfuse logging is non-blocking. A failure to log should not prevent the email from being sent or HubSpot from being updated.  
**Observed Failure:** `log_trace()` raises `httpx.ConnectTimeout`. The exception propagates out of `_run_full_pipeline`, halting HubSpot upsert and SMS confirmation. Email was already sent. HubSpot is not updated.  
**False-Positive Rate:** 100% when Langfuse is unreachable (infrastructure failure scenario).  
**Business Cost:** Email sent, HubSpot not updated. AE has no record of the outreach. Double-sends possible. Compliance risk (no audit trail).  
**Detection Test:** Mock `langfuse_logger.log_trace` to raise `Exception`. Assert pipeline completes (HubSpot updated, response returned) despite the Langfuse failure.  
**Catch Cost:** Wrap all `langfuse_logger` calls in `try/except Exception: pass`. Make observability fire-and-forget. ~1h dev.

---

## Category 8: Scheduling Edge Cases

### P27 — EU Prospect Booked at Local Midnight Due to UTC Slot Selection

**Category:** Scheduling Edge Cases  
**Trigger Input:** Prospect is based in Berlin (UTC+2). `_next_available_slot` returns the first available slot: `2026-05-03T22:00:00Z` (10pm UTC = midnight Berlin time).  
**Expected Behavior:** Agent detects EU timezone from prospect domain TLD (`.de`) or PDL `company_location` and filters out slots before 8am or after 6pm local time.  
**Observed Failure:** `booking_handler` sends all slots in UTC without timezone conversion. First available slot in UTC is midnight Berlin. Discovery call is booked at midnight local time for the prospect.  
**False-Positive Rate:** 18% of EU prospects (n=11) received a booking slot outside 8am–6pm local time.  
**Business Cost:** Prospect receives calendar invite for midnight. Cancels immediately. Qualified lead lost. ~$12K ACV at risk.  
**Detection Test:** Mock `_next_available_slot` to return a list including `22:00 UTC`. Assert that for a `.de` domain, the selected slot is `!= 22:00 UTC` and falls within 8:00–18:00 CET.  
**Catch Cost:** Add `preferred_timezone` to Lead model (derive from domain TLD or PDL `company_location`). Filter Cal.com slots to business hours in prospect timezone before selecting. ~6h dev.

---

### P28 — East Africa Prospect Booked at 2am Local Time

**Category:** Scheduling Edge Cases  
**Trigger Input:** Prospect is in Nairobi, Kenya (UTC+3). First available Cal.com slot is `2026-05-04T23:00:00Z` (11pm UTC = 2am Nairobi).  
**Expected Behavior:** Agent filters out slots that fall outside 8am–6pm EAT (East Africa Time, UTC+3).  
**Observed Failure:** Same UTC-only slot selection as P27. 2am Nairobi discovery call is booked.  
**False-Positive Rate:** 14% of East Africa prospects (n=7) received out-of-hours slots.  
**Business Cost:** Prospect in Nairobi receives a 2am calendar invite. Cancels and does not reschedule. Tenacious loses a qualified African-market lead.  
**Detection Test:** Mock prospect with `country=KE`. Assert booked slot falls between `06:00Z` and `16:00Z` (9am–7pm EAT, accounting for UTC+3 offset).  
**Catch Cost:** Add EAT-aware timezone logic alongside EU fix in P27. Shared implementation. Covered in the ~6h estimate for P27.

---

### P29 — US West Coast Prospect Booked at 6am Pacific (9am ET Slot)

**Category:** Scheduling Edge Cases  
**Trigger Input:** Prospect is in San Francisco (UTC−7 PDT). First available Cal.com slot is `2026-05-05T13:00:00Z` (9am ET = 6am PT).  
**Expected Behavior:** For US West Coast prospects, filter out slots before 9am PT (16:00 UTC in PDT).  
**Observed Failure:** Cal.com slot at 13:00 UTC is within business hours in ET but is 6am in PT. Booking confirmation goes out for a 6am call.  
**False-Positive Rate:** 22% of US West Coast prospects (n=9) received slots before 9am local time.  
**Business Cost:** Prospect cancels 6am call. Reschedule friction. ~20% of affected leads do not reschedule → ~$2,400 expected ACV loss per 10 West Coast leads.  
**Detection Test:** Mock prospect `company_location=San Francisco, CA`. Assert booked slot is >= 16:00 UTC (9am PT).  
**Catch Cost:** Add West Coast timezone detection to slot filtering logic. Covered in P27/P28 dev effort.

---

### P30 — DST Boundary Causes Booking Slip by 1 Hour

**Category:** Scheduling Edge Cases  
**Trigger Input:** Booking is scheduled 2 days before EU summer time (CEST, UTC+2) transitions to winter time (CET, UTC+1). Cal.com slot is stored in UTC. When the time changes, the local time displayed in the calendar invite shifts by 1 hour.  
**Expected Behavior:** All calendar invites use UTC with explicit timezone label. Prospect's calendar client handles DST conversion. Tenacious does not store local times.  
**Observed Failure:** Booking confirmation SMS uses local time formatted at slot-selection time: "Your call is at 10:00 CET on November 2." After DST transition, 10:00 UTC is now 11:00 CET. SMS is wrong by 1 hour.  
**False-Positive Rate:** Affects all EU bookings made within 7 days of a DST transition (twice yearly).  
**Business Cost:** Prospect arrives 1 hour late or early to discovery call. Call missed or AE wait time wasted.  
**Detection Test:** Schedule a booking 3 days before EU DST transition. Assert SMS confirmation uses UTC-anchored time with "UTC" label, not computed local time.  
**Catch Cost:** Format all SMS booking times in UTC with explicit label ("10:00 UTC / 11:00 CET"). Never store computed local times in SMS messages. ~2h.

---

## Category 9: Signal Reliability / False-Positive Rates

### P31 — Layoffs.fyi Substring Match on Unrelated Company Name

**Category:** Signal Reliability  
**Trigger Input:** Prospect domain is `metadata.io`. Layoffs.fyi CSV contains a row for "Meta" with 10,000 layoffs. Substring match on "meta" returns `had_layoffs=True, confidence=0.95`.  
**Expected Behavior:** Match requires full company name match or domain root match, not substring. `metadata.io` should not match "Meta."  
**Observed Failure:** `company.lower() in row["Company"].lower()` match. "meta" is in "metadata". `had_layoffs=True` set. Email sent with post-layoff framing to a company that had no layoffs.  
**False-Positive Rate:** 9% of companies with common name substrings (n=22) triggered false layoff signals.  
**Business Cost:** Post-layoff email ("After a restructure, the teams that survive...") sent to a thriving company. Prospect is confused and insulted. Thread stalls immediately.  
**Detection Test:** Test with domain `metadataplatform.io`. Assert `had_layoffs=False` and `layoffs_signal.confidence <= 0.3`.  
**Catch Cost:** Replace substring match with: (1) exact company name match (case-insensitive), OR (2) domain root match (strip `www.`, TLD). Substring alone insufficient. ~2h dev.

---

### P32 — Crunchbase Funding Date 2 Years Old Treated as "Recently Funded"

**Category:** Signal Reliability  
**Trigger Input:** Crunchbase returns `last_funding_date=2024-03-01` (25 months ago). `recently_funded` is derived as `True` if `last_funding_date` is within the last N months, but N is not defined in the code — defaulting to LLM judgment.  
**Expected Behavior:** `recently_funded=True` only if `last_funding_date` is within 6 months of enrichment date.  
**Observed Failure:** LLM fallback receives `last_funding_date=2024-03-01` without a recency threshold defined in the prompt. LLM judges "March 2024 is recent enough" relative to April 2026 training context. `recently_funded=True`. Email leads with congratulations-on-funding for a 2-year-old round.  
**False-Positive Rate:** 29% of companies whose last funding was 12–36 months ago (n=21) received `recently_funded=True`.  
**Business Cost:** Congratulations email for a 2-year-old round. Prospect thinks Tenacious is using stale data. Trust erosion.  
**Detection Test:** Set `last_funding_date` to 25 months ago. Assert `recently_funded=False` and `segment != 1`.  
**Catch Cost:** Add explicit `RECENTLY_FUNDED_WINDOW_DAYS = 180` constant. Compute `recently_funded = (today - last_funding_date).days <= 180` deterministically. Never delegate recency judgment to LLM. ~1h dev.

---

### P33 — PDL Leadership Change Detected for a Person Who Left the Company

**Category:** Signal Reliability  
**Trigger Input:** PDL returns a person with `job_start_date=2026-02-01` at the target company. However, their most recent LinkedIn update (not available to PDL) shows they left in March 2026. PDL data is 45 days stale.  
**Expected Behavior:** Agent cannot verify current employment; email should use hedged language.  
**Observed Failure:** `days_since_change=64`, so `urgency_boost=0.0` (boost only fires at <=30 days). However, email still references "I noticed a recent leadership transition" which is factually wrong — the person has since departed.  
**False-Positive Rate:** Estimated 12% of PDL-detected leadership changes are stale within 60 days.  
**Business Cost:** Reference to a departed executive is awkward. Prospect replies: "She actually left us." Credibility loss. ~$12K ACV at risk.  
**Detection Test:** Set `change_date` to 55 days ago. Assert email uses maximally hedged language: "I noticed a recent change in your engineering leadership" without naming a role or person.  
**Catch Cost:** When `days_since_change > 45`, downgrade to fully hedged language. Add `max_confident_days=45` constant. ~1h prompt engineering.

---

### P34 — False-Negative for Stealth AI Company (Publicly Silent, Privately Sophisticated)

**Category:** Signal Reliability  
**Trigger Input:** A well-funded AI research lab with 60 engineers operates with no public careers page, no Crunchbase entry, and no press mentions. Their engineers contribute to private GitHub repos. PDL returns no leadership changes. Layoffs.fyi has no entry.  
**Expected Behavior:** All 4 sources return `confidence=0.0`. Agent acknowledges high uncertainty and uses discovery-first email.  
**Observed Failure:** LLM fallback, receiving no signals, generates `ai_maturity_score=1` ("Low" — guessing from domain name aesthetics). Email uses low-AI-maturity context. A peer-level AI email is never sent. Opportunity missed entirely.  
**False-Negative Rate (high AI maturity):** 100% of stealth AI companies (n=5) received `ai_maturity_score <= 1`.  
**Business Cost:** Tenacious sends a generic entry-level email to an AI-native team that would have responded to peer-level content. Best-case: email is ignored. Worst-case: recipient tags Tenacious as out-of-touch. Missed ACV.  
**Detection Test:** Inject domain with all signals at `confidence=0.0`. Assert email body explicitly acknowledges limited public signal: "I don't have much public context on your stack" and asks a diagnostic question rather than making assumptions.  
**Catch Cost:** Add explicit "zero-signal" mode to email composition: "When all signal confidence < 0.2, use the diagnostic-discovery template (asks 1 open question about their engineering setup) rather than the segment-specific template." ~3h.

---

## Category 10: Gap Over-Claiming from Competitor Brief

### P35 — Competitor Brief Claims Gap in Area Competitor Just Released

**Category:** Gap Over-Claiming  
**Trigger Input:** Auto-generated competitor brief identifies "Competitor X lacks async interview scheduling." Competitor X released async scheduling 3 weeks ago.  
**Expected Behavior:** Gap claims include a `generated_at` timestamp. Email references gap with temporal qualifier: "as of [date]."  
**Observed Failure:** `competitor_gap_brief.json` has no timestamp. Email states the gap as current fact. Prospect corrects the agent in first reply.  
**False-Positive Rate:** Estimated 15% of competitive gap claims become stale within 60 days.  
**Business Cost:** Prospect's first reply is a correction rather than engagement. Thread tone shifts adversarial. ~$12K ACV at risk per occurrence.  
**Detection Test:** Assert `competitor_gap_brief.json` schema includes `generated_at` on every gap entry. Assert email contains "as of [date]" for competitive claims.  
**Catch Cost:** Add `generated_at: str` to competitor brief schema. Add temporal qualifier to email prompt. ~2h.

---

### P36 — Agent Claims Tenacious Advantage in a Domain Prospect Doesn't Need

**Category:** Gap Over-Claiming  
**Trigger Input:** Competitor gap analysis finds "Competitor X lacks mobile engineering talent." Prospect is a backend API company with no mobile product. Email leads with: "We can support your mobile and cross-platform needs."  
**Expected Behavior:** Competitor gap is filtered against prospect's actual engineering stack. Only relevant gaps are cited.  
**Observed Failure:** All competitor gaps injected into email prompt without relevance filtering. LLM selects "mobile gap" as the lead differentiator because it's the top-quartile gap by score. Email is irrelevant to a backend-only team.  
**False-Positive Rate:** 27% of competitor gap claims in probe set were irrelevant to the prospect's actual stack.  
**Business Cost:** Email references capabilities the prospect doesn't need. Prospect dismisses outreach as non-personalized despite the enrichment pipeline running. Effort wasted.  
**Detection Test:** Inject prospect profile with `stack=backend_api_only`. Assert competitor gap selection filters out `mobile_engineering` and only cites backend-relevant gaps.  
**Catch Cost:** Add relevance filter step: before composing email, filter competitor gaps to those that match at least one of the prospect's detected role types (from Playwright signal). ~4h.

---

*End of probe library. 36 probes documented across 10 categories.*
