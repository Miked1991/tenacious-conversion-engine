"""
Sends 20 synthetic messages to the running agent server and measures end-to-end
wall-clock latency for each request, then reports p50 and p95.

Usage:
  python scripts/measure_latency.py [--base-url http://localhost:8000]
"""

import argparse
import json
import math
import time

import httpx

SYNTHETIC_LEADS = [
    ("alice.chen@techstartup.io", "Tell me more about your engineering teams."),
    ("bob.smith@growthco.ai", "How do you handle rapid scaling?"),
    ("carol.jones@fundednow.com", "We just closed our Series B. What can you offer?"),
    ("david.lee@leancorp.net", "We're running lean after layoffs. What's the ROI?"),
    ("eve.martin@scalefast.dev", "Our headcount doubled this year. Can you keep up?"),
    ("frank.wu@techstartup.io", "What's your typical onboarding timeline?"),
    ("grace.kim@growthco.ai", "Do you work with companies our size?"),
    ("henry.patel@fundednow.com", "What does a typical engagement look like?"),
    ("iris.nguyen@leancorp.net", "Can you integrate with our existing tools?"),
    ("jack.brown@scalefast.dev", "What metrics do you track for success?"),
    ("karen.white@innovate.io", "We need to ship faster. How do you help?"),
    ("liam.davis@nextseries.ai", "Tell me about your pricing model."),
    ("mia.wilson@turbogrowth.com", "What's your NPS from current clients?"),
    ("noah.taylor@buildfast.dev", "Do you offer a pilot program?"),
    ("olivia.anderson@stepup.co", "We have 80 engineers. Is that your sweet spot?"),
    ("peter.thomas@funded2024.io", "When can we get started?"),
    ("quinn.jackson@agile.ai", "What's the typical contract length?"),
    ("rachel.harris@scale2x.dev", "Can you share a case study?"),
    ("sam.martin@hyperco.io", "How quickly can you ramp up?"),
    ("tara.garcia@momentum.ai", "What makes you different from competitors?"),
]


def _percentile(data: list[float], pct: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    idx = (pct / 100) * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (idx - lo) * (s[hi] - s[lo])


def measure(base_url: str) -> dict:
    latencies = []
    results = []

    print(f"Sending {len(SYNTHETIC_LEADS)} requests to {base_url}/simulate ...")
    for email, text in SYNTHETIC_LEADS:
        t0 = time.perf_counter()
        try:
            resp = httpx.post(
                f"{base_url}/simulate",
                json={"email": email, "text": text},
                timeout=60,
            )
            elapsed = time.perf_counter() - t0
            ok = resp.status_code == 200
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            ok = False
        latencies.append(elapsed)
        results.append({"email": email, "ok": ok, "latency_s": round(elapsed, 3)})
        status = "OK" if ok else "ERR"
        print(f"  [{status}] {email:<40} {elapsed:.2f}s")

    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    report = {
        "n": len(latencies),
        "p50_s": round(p50, 3),
        "p95_s": round(p95, 3),
        "min_s": round(min(latencies), 3),
        "max_s": round(max(latencies), 3),
        "details": results,
    }
    print()
    print(f"p50 latency: {p50:.3f}s")
    print(f"p95 latency: {p95:.3f}s")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()
    report = measure(args.base_url)
    out = "latency_report.json"
    with open(out, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved to {out}")
