# tau2-bench Retail Baseline

## What was reproduced

The retail domain baseline was run using tau2-bench's built-in `llm_agent` against 30 tasks
drawn from the `test` split, repeated across 5 trials each (150 simulations total). The agent
model was `qwen/qwen3-next-80b-a3b-instruct` via OpenRouter; the user simulator used
`openai/gpt-4.1` via the same gateway. No infrastructure errors occurred.

## Results

| Metric | Value |
| --- | --- |
| pass@1 | **0.7267 (72.67 %)** |
| 95 % CI | [0.6504, 0.7917] |
| Evaluated simulations | 150 |
| Total tasks | 30 |
| Trials per task | 5 |
| Avg agent cost | $0.0199 / simulation |
| p50 latency (tau2) | 105.95 s |
| p95 latency (tau2) | 551.65 s |
| Infrastructure errors | 0 |
| Git commit | `d11a97072c49d093f7b5a3e4fe9da95b490d43ba` |

## Production agent latency (20 live interactions)

The FastAPI conversion agent was measured over 20 synthetic leads through the full
pipeline (enrich → compose → tone-check → Resend → HubSpot sync):

| Metric | Value |
| --- | --- |
| p50 | **29.3 s** |
| p95 | **36.3 s** |
| min | 27.1 s |
| max | 38.4 s |
| Success rate | 20 / 20 |

## Cost

At $0.0199 per simulation across 150 runs the total evaluation spend was approximately
**$2.99**, well inside the $12 target. Mean cost per qualified lead is below $5.

## Confidence interval

The Wilson score 95 % CI of [0.6504, 0.7917] spans ~14 percentage points, reflecting
natural LLM variance across 5 trials. Widening to 10 trials would narrow this to ~+/-6 pp.

## Unexpected behaviour

- The correct OpenRouter slug is `qwen/qwen3-next-80b-a3b-instruct`; the guide omits
  `-instruct`, causing every task to fail with HTTP 400 until corrected.
- p95 tau2 latency (551 s) greatly exceeds p50 (106 s), indicating a long tail of hard
  multi-step tasks where the agent exhausted its turn budget. A lower `--max-steps` cap
  or a tighter system prompt would reduce this.
- A small number of tasks ended with the user simulator timing out rather than confirming
  completion; these were scored as failures and excluded from the pass@1 numerator.
