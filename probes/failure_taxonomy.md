# Failure Taxonomy — Tenacious Conversion Engine

**Version:** 1.0 | **Date:** 2026-04-25 | **Author:** Mikias Dagem  
**Source:** probe_library.md (36 probes)

This document groups all probes from the probe library into 10 failure categories, reports observed trigger rates from probe evaluation, and ranks categories by business-cost impact for Tenacious.

---

## Taxonomy Summary Table

| Category | Probes | Trigger Rate | Severity | ACV Risk / Occurrence |
|---|---|---|---|---|
| ICP Misclassification | P01–P05 | 18–41% | Critical | $3,000–$20,000 |
| Hiring-Signal Over-Claiming | P06–P10 | 8–34% | High | $12,000 |
| Bench Over-Commitment | P11–P13 | 20–100% | Critical | $12,000–$60,000 |
| Tone Drift | P14–P17 | 12–38% | Medium | Brand erosion |
| Multi-Thread Leakage | P18–P20 | 3–100% | Critical | GDPR exposure |
| Cost Pathology | P21–P23 | 2–15% | Medium | $94/mo at scale |
| Dual-Control Coordination | P24–P26 | 6–100% | High | $12,000 / lead lost |
| Scheduling Edge Cases | P27–P30 | 14–22% | Medium | $2,400–$12,000 |
| Signal Reliability | P31–P34 | 9–100% | High | $12,000 / missed |
| Gap Over-Claiming | P35–P36 | 15–27% | Medium | $12,000 |

---

## Category 1: ICP Misclassification (Probes P01–P05)

**Definition:** The agent misidentifies the prospect's company type, funding stage, or growth profile, causing the wrong segment to be applied and the wrong email template to be sent.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P01 | Bootstrapped Company Flagged as Recently-Funded | 23% |
| P02 | Post-Layoff Company Classified as Hypergrowth | 31% |
| P03 | IT Staffing Competitor Misidentified as ICP Prospect | 18% |
| P04 | Pre-Revenue 2-Person Startup Fast-Tracked as High-Value | 41% |
| P05 | Stealth-Mode AI Company Scored as Low AI Maturity | 100% (stealth subset) |

**Root Cause Pattern:** Segment classification relies on `_classify_segment()`, which applies a waterfall of conditions. When primary signals (Crunchbase, PDL) are absent and the LLM fallback is given too much latitude, it infers segment based on proxy signals (high job-post count alone, domain aesthetics) that are insufficient.

**Observed Aggregate Trigger Rate:** 22% of probe-set leads misclassified in at least one segment dimension.

**Tenacious-Specific Severity:** ICP misclassification for Tenacious is particularly costly because Tenacious sells a premium service (engineering team augmentation at $12K+ ACV). Sending a hypergrowth email to a bootstrapped 3-person company or a funding-congratulations email to a cost-cutting company immediately signals poor research — the exact opposite of Tenacious's value proposition.

**Systemic Fix Priority:** HIGH. These are the most common failures and compound other categories (wrong segment → wrong tone → wrong bench commitment).

---

## Category 2: Hiring-Signal Over-Claiming (Probes P06–P10)

**Definition:** The agent makes specific claims about a company's hiring activity, AI maturity, or competitive position that are unsupported, stale, or over-interpreted from weak signals.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P06 | Single Stale Job Post Claimed as "Aggressive Hiring" | 34% |
| P07 | Playwright Follows Redirect to ATS, Inflates Role Count | 22% |
| P08 | Leadership Change Email Sent After Executive Departed | ~8% |
| P09 | Data Analyst Roles Inflate AI Maturity Score | 28% |
| P10 | Competitor Brief Gap Is Stale (Competitor Just Shipped) | ~15% |

**Root Cause Pattern:** The Playwright job-scrape signal (confidence=0.8) is given high weight in the LLM prompt, but the signal has several unvalidated assumptions: (a) the timestamp of the posting is not checked, (b) the job board domain is not validated against known ATS redirects, (c) role title regex is too broad.

**Observed Aggregate Trigger Rate:** 21% of probe-set leads received at least one over-claimed hiring signal.

**Tenacious-Specific Severity:** Tenacious's outreach is grounded in the promise of *research-backed* personalization. An email that references stale or wrong hiring signals is worse than a generic email — it signals to the prospect that the "research" is fabricated. This damages Tenacious's core brand promise.

**False-Positive Rate by Signal:**
- Stale job posts (P06): 34%
- ATS redirect inflation (P07): 22%  
- AI maturity over-score (P09): 28%
- Leadership-change staleness (P08): ~8%

**Systemic Fix Priority:** HIGH. Over-claiming is Tenacious's most visible public-facing risk.

---

## Category 3: Bench Over-Commitment (Probes P11–P13)

**Definition:** The agent's outreach implies Tenacious can staff team sizes, specialties, or geographies that exceed the actual bench capacity.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P11 | Implies 10-Person ML Team Available in 2 Weeks | 100% (segment=3 emails) |
| P12 | Pitches Quantum Computing Bench Tenacious Doesn't Have | ~100% when niche detected |
| P13 | Overpromises for EU-Regulated Roles | 100% (EU prospects) |

**Root Cause Pattern:** Email composition prompts for segment=3 (hypergrowth) and segment=1 (recently_funded) use scaling language ("build out rapidly," "scale your team") without connecting to bench availability. The system has no bench-inventory API or constraint.

**Observed Aggregate Trigger Rate:** ~35% of all outbound emails contain at least one implied commitment on team size or timeline.

**Tenacious-Specific Severity:** CRITICAL. When a prospect reaches the discovery call expecting a commitment the agent implied, the AE must either break the expectation (damaging trust) or overpromise further (creating delivery risk). Bench over-commitment is the failure mode most likely to damage Tenacious's delivery reputation at scale.

**Business Cost Derivation:**
- Assume 5% of hypergrowth leads (segment=3) reach discovery call with a bench-mismatch expectation
- At 150 leads/month: 150 × 15% hypergrowth × 5% mismatch rate = 1.1 calls/month with expectation mismatch
- At 50% ACV loss per mismatch: 1.1 × $12,000 × 50% = **$6,600/month risk at scale**

**Systemic Fix Priority:** CRITICAL. Requires bench-availability API integration as a long-term fix; prompt-level hedging as an immediate fix.

---

## Category 4: Tone Drift (Probes P14–P17)

**Definition:** Outreach email or conversation reply deviates from the Tenacious style guide (warm, direct, human, no buzzwords, <120 words, no "AI" or "disruption").

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P14 | Email Contains Jargon Words Banned by Style Guide | 17% |
| P15 | Email Exceeds 120-Word Limit | 24% |
| P16 | Reply Tone Escalates to Pushy After 2 Turns | 38% |
| P17 | Casual Slang Inappropriate for CTO Audience | 12% |

**Root Cause Pattern:** The tone-check LLM (binary YES/NO) is not sufficiently sensitive to subtle violations. It catches extreme violations but misses (a) single banned words in otherwise-clean emails, (b) word-count overruns, and (c) tone escalation in replies (tone-check is only applied to initial outreach, not conversation replies).

**Observed Aggregate Trigger Rate:** 33% of all emails and replies contained at least one tone violation that was not caught by the LLM tone-check.

**Tenacious-Specific Severity:** Tenacious's style guide is a core brand asset. Violation is low-cost per instance but cumulative reputational damage at scale. Style guide violations are also the easiest failure mode to fix deterministically (regex + word count).

**Systemic Fix Priority:** MEDIUM. Easy to fix with deterministic checks; LLM tone-check alone is insufficient.

---

## Category 5: Multi-Thread Leakage (Probes P18–P20)

**Definition:** Lead state from one prospect contaminates another's profile, phone lookup, or suppression status.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P18 | In-Memory Lead State Collision (Concurrent Async) | 7% (concurrent scenarios) |
| P19 | SMS Confirmation Sent to Wrong Phone (E.164 Mismatch) | 3% (East African numbers) |
| P20 | Bounced Lead Still Receives Conversation Replies | 100% (bounce + replay) |

**Root Cause Pattern:** The in-memory `_LEADS` dict is not thread-safe under concurrent async load. Phone number lookup uses non-normalized keys. Bounce suppression state is not propagated back to the in-memory dict.

**Observed Aggregate Trigger Rate:** Low base rate (3–7%) but 100% failure in the specific conditions (bounce + replay, concurrent load).

**Tenacious-Specific Severity:** CRITICAL. Multi-thread leakage creates GDPR data-handling violations (sending one lead's data to another). Personal data cross-contamination exposes Tenacious to regulatory risk. Sending to bounced addresses damages Resend sender reputation, which affects all Tenacious outreach.

**Systemic Fix Priority:** CRITICAL. The in-memory dict must be replaced with Redis before production. Immediate: add per-email locks and bounce suppression in memory.

---

## Category 6: Cost Pathology (Probes P21–P23)

**Definition:** The agent incurs excessive LLM or API costs due to retry loops, browser hangs, or duplicate CRM records.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P21 | Tone-Check Retry Loop Exceeds 1 Retry | 2% |
| P22 | Playwright Hangs on JS-Heavy Careers Page | 15% |
| P23 | HubSpot Duplicate Contact on Email Case Mismatch | 11% |

**Root Cause Pattern:** (a) No hard cap on retry count, (b) no timeout on Playwright page loads, (c) no email normalization before HubSpot key lookup.

**Observed Aggregate Trigger Rate:** 18% of leads triggered at least one cost pathology.

**Tenacious-Specific Severity:** MEDIUM at evaluation scale; HIGH at production scale. At 1,000 leads/month with 15% Playwright hangs: 150 leads with 45s excess latency = 112 minutes of blocked async capacity per month. At 11% HubSpot duplication: 110 duplicate contacts cluttering CRM — operational overhead for Tenacious AEs.

**Systemic Fix Priority:** MEDIUM. Simple deterministic fixes available for all three.

---

## Category 7: Dual-Control Coordination (Probes P24–P26)

**Definition:** Two services (Cal.com, HubSpot, Langfuse) go out of sync when one call succeeds and another fails.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P24 | Cal.com Slot Double-Booked Under Concurrent Load | 15% (concurrent) |
| P25 | HubSpot CONNECTED Without Booking URL | 6% |
| P26 | Email Sent But Langfuse Trace Not Written | 100% (Langfuse outage) |

**Root Cause Pattern:** The pipeline is not transactional. Individual API calls succeed or fail independently. There is no rollback mechanism when a later step fails.

**Observed Aggregate Trigger Rate:** 9% of probe-set leads left HubSpot in an inconsistent state.

**Tenacious-Specific Severity:** HIGH. HubSpot is the AE's source of truth. Inconsistent CRM state causes AEs to make incorrect follow-up decisions (e.g., calling a "CONNECTED" lead with no meeting context). This is an operational risk, not just a technical one.

**Systemic Fix Priority:** HIGH. Immediate fix for P25 is trivial (conditional status update). P24 requires slot caching. P26 requires non-blocking Langfuse calls.

---

## Category 8: Scheduling Edge Cases (Probes P27–P30)

**Definition:** Discovery call bookings land outside business hours for prospects in EU, East Africa, or US West Coast due to UTC-only slot selection.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P27 | EU Prospect Booked at Local Midnight | 18% |
| P28 | East Africa Prospect Booked at 2am Local | 14% |
| P29 | US West Coast Booked at 6am Pacific | 22% |
| P30 | DST Boundary Causes Slot to Slip 1 Hour | Twice yearly |

**Root Cause Pattern:** `booking_handler` selects the first available Cal.com slot in UTC without applying timezone constraints derived from prospect location.

**Observed Aggregate Trigger Rate:** 18% of all probe-set bookings fell outside 9am–6pm local time for the prospect.

**Tenacious-Specific Severity:** MEDIUM-HIGH. Tenacious operates across East Africa (primary market), EU, and US. Timezone awareness is especially critical for the East Africa segment where time differences with US/EU are large and business hours overlap is limited.

**Systemic Fix Priority:** MEDIUM. Single shared fix: add `preferred_timezone` to Lead, filter Cal.com slots accordingly.

---

## Category 9: Signal Reliability / False-Positive Rates (Probes P31–P34)

**Definition:** Data signals from enrichment sources return incorrect, stale, or fabricated values that corrupt downstream segment classification and email content.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P31 | Layoffs.fyi Substring Match on Wrong Company | 9% |
| P32 | Crunchbase Funding Date 2 Years Old Treated as Recent | 29% |
| P33 | PDL Leadership Change Stale by 45 Days | ~12% |
| P34 | Stealth AI Company Scored as Low Maturity (False Negative) | 100% (stealth subset) |

**Root Cause Pattern:** Signal validation is insufficiently strict. Recency thresholds are not enforced deterministically — they are delegated to LLM judgment. Company name matching uses substring logic instead of exact or domain-root matching.

**Observed Aggregate Trigger Rate:** 15% of probe-set leads received at least one signal value that was materially incorrect.

**Tenacious-Specific Severity:** HIGH. Signal reliability is the foundation of the entire system's value claim. If signals are wrong, all downstream personalization is wrong — and worse than no personalization, because it creates false specificity. For stealth AI companies (P34), the false-negative means Tenacious systematically misses its highest-potential prospects (AI-native companies are Tenacious's best ICP).

**Systemic Fix Priority:** HIGH. Deterministic recency checks, exact-match company lookup, and zero-signal mode detection are all straightforward fixes.

---

## Category 10: Gap Over-Claiming from Competitor Brief (Probes P35–P36)

**Definition:** The competitor gap analysis cites differentiators that are stale, irrelevant to the prospect's actual stack, or not supported by current competitive intelligence.

**Probes in this category:**

| ID | Name | Trigger Rate |
|---|---|---|
| P35 | Competitor Gap Stale — Competitor Just Released the Feature | ~15% |
| P36 | Competitor Advantage Cited Is Irrelevant to Prospect Stack | 27% |

**Root Cause Pattern:** Competitor briefs are generated once and not refreshed. There is no relevance filter connecting gap claims to the prospect's detected engineering stack.

**Observed Aggregate Trigger Rate:** 21% of probe-set competitor gap claims were either stale or irrelevant.

**Tenacious-Specific Severity:** MEDIUM. Irrelevant gap claims weaken the email's perceived personalization. Stale gap claims actively harm credibility if the prospect is aware of competitor product releases.

**Systemic Fix Priority:** MEDIUM. Add `generated_at` timestamp and relevance filter.

---

## Ranked Priority List for Production Hardening

Based on trigger rate × business cost:

| Rank | Category | Trigger Rate | ACV Risk | Priority |
|---|---|---|---|---|
| 1 | Bench Over-Commitment (P11–P13) | 35% | $6,600/mo at scale | CRITICAL |
| 2 | Multi-Thread Leakage (P18–P20) | 7–100% | GDPR + ACV | CRITICAL |
| 3 | ICP Misclassification (P01–P05) | 22% | $3K–$20K/case | CRITICAL |
| 4 | Hiring-Signal Over-Claiming (P06–P10) | 21% | $12K/case | HIGH |
| 5 | Dual-Control Coordination (P24–P26) | 9% | $12K/lead lost | HIGH |
| 6 | Signal Reliability (P31–P34) | 15% | $12K + missed ACV | HIGH |
| 7 | Tone Drift (P14–P17) | 33% | Brand erosion | MEDIUM |
| 8 | Scheduling Edge Cases (P27–P30) | 18% | $2.4K–$12K | MEDIUM |
| 9 | Gap Over-Claiming (P35–P36) | 21% | $12K/case | MEDIUM |
| 10 | Cost Pathology (P21–P23) | 18% | $94/mo at scale | MEDIUM |

---

*Total probes: 36 across 10 categories. All probes are Tenacious-specific — each failure mode reflects the actual risk surface of a talent-outsourcing outreach agent operating across East Africa, EU, and US markets.*
