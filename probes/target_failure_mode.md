# Target Failure Mode — Highest-ROI for Tenacious

**Version:** 1.0 | **Date:** 2026-04-25 | **Author:** Mikias Dagem

---

## Single Highest-ROI Failure Mode: Bench Over-Commitment

**Probe IDs:** P11, P12, P13  
**Category:** Bench Over-Commitment  
**Trigger Rate:** ~35% of all outbound emails (100% of hypergrowth segment, ~20% of recently-funded segment)

---

## What It Is

The agent's outreach email uses scaling language — "build out your team rapidly," "scale to 10 engineers," "available quickly" — without any constraint on Tenacious's actual bench availability, staffing specialties, or geographic compliance requirements.

When the prospect reads the email and reaches the discovery call, they arrive with a concrete expectation set by the agent: *Tenacious can staff a specific team size, in a specific specialty, on a specific timeline*. If the actual bench cannot fulfill that expectation, the discovery call collapses — not because Tenacious lacked the relationship, but because the agent overclaimed.

This is distinct from a bad email (which the prospect ignores) or a missed ICP (which is caught before a call). Bench over-commitment gets *past* the conversion funnel and creates damage at the point of highest-value interaction: the discovery call.

---

## Why τ²-Bench Does Not Capture This

τ²-bench evaluates retail task completion using a simulated user and customer-service task domain. Its reward signal measures whether a transaction is completed (e.g., order return processed, discount applied). It has no concept of:

1. **Bench availability constraints** — τ²-bench has no external inventory system the agent must consult before making a commitment. In Tenacious's world, the agent would need a bench API (or bench-availability table) to verify that "10 ML engineers in 2 weeks" is fulfillable.

2. **Discovery-call collapse** — τ²-bench terminates at task completion or timeout. It does not simulate a downstream consequence where an implied commitment causes a human meeting to fail. The reward=1.0 for a τ²-bench task does not encode whether the real-world equivalent would produce a successful discovery call.

3. **Multi-step trust dynamics** — τ²-bench rewards within a single session. Bench over-commitment creates damage in a *second* session (the discovery call), which is entirely outside τ²-bench's evaluation window.

4. **Segment-specific capability claims** — τ²-bench retail tasks are generic (return an order, apply a coupon). They do not test whether a claim made in an outreach email (segment=hypergrowth → "scale fast") is bounded by real-world delivery constraints.

**What would need to be added to catch this:**
- A bench-availability oracle that the agent must call before making capacity claims
- A second evaluation stage: after the "booking confirmed" reward, simulate the discovery call with a skeptical user who asks "you mentioned 10 engineers in 2 weeks — can you confirm?"
- A reward penalty if the agent cannot fulfill the expectation it set in the outreach email

**Cost to add:** ~40h engineering (bench-oracle stub + second-stage evaluation loop). Bench data must come from Tenacious ops team.

---

## Business-Cost Derivation in Tenacious Terms

### Base Assumptions (from Tenacious brief and evaluation data)
- Average Contract Value (ACV): $12,000 (Tenacious's standard talent placement contract, 1 engineer × 3-month engagement)
- Stalled-thread rate (manual process): 30–40%
- System-measured stalled-thread rate: 27.3% (1 − pass@1 = 1 − 0.7267, from eval/score_log.json)
- Leads per month (pilot scope): 30
- Hypergrowth segment fraction: ~15% of leads = 4–5 per month

### Mechanism of Loss

Step 1 — Over-commitment rate: 100% of hypergrowth-segment emails (segment=3) contain at least one unqualified scaling claim ("build out rapidly," "available in weeks"). Confirmed by probe P11 detection test across all segment=3 compose outputs.

Step 2 — Discovery call reach rate: Of hypergrowth leads that reply, ~60% reach a discovery call (based on system's 74% pass@1 × segment=3 reply rate of ~82% = 61% booking rate).

Step 3 — Bench mismatch rate at discovery: Estimate 20% of discovery calls for segment=3 leads reveal a mismatch between what the email implied and what Tenacious can actually deliver (specialty not on bench, timeline not achievable, EU compliance issue).

Step 4 — ACV loss per mismatch: When discovery collapses due to overclaimed expectations, the prospect does not convert (100% ACV loss for that deal). Additionally, ~30% of these prospects share negative feedback in peer networks, creating indirect brand-reputation cost.

### Monthly Expected Loss Calculation

```
leads/month           = 30
hypergrowth leads     = 30 × 15% = 4.5
reach discovery call  = 4.5 × 61% = 2.7 calls/month
bench mismatch        = 2.7 × 20% = 0.54 call collapses/month
ACV lost per collapse = $12,000
Expected ACV loss     = 0.54 × $12,000 = $6,480/month
Annual ACV at risk    = $6,480 × 12 = $77,760
```

At 200 leads/month (Tenacious's growth target for a mature pilot):
```
hypergrowth leads     = 200 × 15% = 30
reach discovery call  = 30 × 61% = 18.3 calls/month
bench mismatch        = 18.3 × 20% = 3.7 collapses/month
Expected ACV loss     = 3.7 × $12,000 = $44,400/month
Annual ACV at risk    = $532,800
```

### Brand-Reputation Multiplier

Tenacious's market is concentrated: founders, CTOs, and VPs Engineering in startup ecosystems (East Africa, EU, US) where professional networks are tight. A discovery call that collapses because "the agent overpromised" is a story that gets shared. Conservative estimate: 30% of bench-mismatch cases generate a negative mention in a Slack community, LinkedIn comment, or referral conversation.

At 3.7 collapses/month: 1.1 negative mentions/month × network reach of ~50 relevant decision-makers per mention = 55 potential Tenacious-negative impressions/month. At a $300 cost-per-impression in targeted B2B advertising, this represents $16,500/month in brand-reputation cost at mature scale.

**Total monthly cost at mature scale (200 leads/month):**
- Direct ACV loss: $44,400
- Brand-reputation cost: $16,500
- **Total: $60,900/month = $730,800/year**

---

## Fix Cost and ROI

### Immediate Fix (Prompt-Level Hedge)
**Implementation:** Add to all segment=1 and segment=3 email composition prompts:
> "Do not commit to specific team sizes, timelines, or specialties. If scaling capacity is relevant, say: 'We'd scope this together on a 30-minute call — we've done this with teams at similar stages.' Never say 'available in X weeks' or 'build out a team of N.'"

**Cost:** 1 hour of prompt engineering.  
**Risk reduction:** Eliminates 80% of bench over-commitment trigger (removes specific scaling claims from email output).  
**Residual risk:** LLM may still generate implied commitments in edge cases. Deterministic check needed.

### Full Fix (Deterministic Email Guard + Bench API)
**Implementation:**
1. Add post-compose regex check for forbidden commitment phrases: `["in [0-9]+ weeks", "team of [0-9]+", "available immediately", "build out quickly", "scale to [0-9]+"]`
2. Integrate bench-availability stub API: before composing for segment=3, query `/bench/availability?specialty=...&size=...` and inject result into prompt as a hard constraint
3. Add test: for every segment=3 email, assert none of the forbidden phrases are present

**Cost:** ~8 hours of engineering (regex guard: 1h, bench API stub: 6h, tests: 1h).  
**Risk reduction:** Near-100% elimination of specific commitment over-claims.

**ROI (at mature scale):**
- Fix cost: 8 engineer-hours × $150/hr = $1,200 one-time
- Monthly savings: $60,900
- Break-even: < 1 day of operation at mature scale
- **Annual ROI: 60,850%**

---

## Summary

Bench over-commitment is the highest-ROI failure mode to fix for Tenacious because:

1. **It is pervasive** — 100% of hypergrowth segment emails currently trigger it
2. **It strikes at the highest-value moment** — the discovery call, after a lead has already been nurtured through enrichment, qualification, and booking
3. **It is invisible to τ²-bench** — the benchmark's evaluation window closes before the downstream damage occurs
4. **It is cheap to fix** — a 1-hour prompt change eliminates 80% of the risk immediately, with a full 8-hour fix achieving near-100% reduction
5. **The financial cost at scale is catastrophic** — $730K/year in direct ACV loss plus brand-reputation damage in concentrated professional networks where Tenacious must build and protect its reputation as a trusted engineering partner

*This is the failure mode a Tenacious CEO should want fixed before any pilot scale-up.*
