# Gate and CI

The gate is fail-closed. DeepTeam’s SDK default `ignore_errors=True` is **not** copied. CI must not inherit a “keep going” SDK default.

`evaluate_gate(assessment, ...)` raises `GateFailure` with every failing reason joined by `"; "`. The CLI prints it in red and exits `1`.

## Error kinds

| `error_kind` | Source | Default gate |
| --- | --- | --- |
| `infrastructure` | `ClientError`: HTTP, timeout, connect, non-JSON, schema, failed run status | Fail if `fail_on_infrastructure_error` (default true) |
| `target` | Exception from `model_callback` | Fail unless `ignore_target_errors` |
| `null` (scored) | Evaluator returned `0` or `1` | Fail **only** if the type is `gated` and `pass_rate < threshold` |

Parse misses and non-binary scores are infrastructure, not `score=0`.

## Matrix

| Event | Test case | Gated vuln (leakage, agency) | Report-only vuln (toxicity, …) |
| --- | --- | --- | --- |
| HTTP 4xx/5xx, timeout, transport | `error` + `infrastructure` | Job **fails** | Job **fails** |
| Schema/parse miss, non-binary score, failed run status | `error` + `infrastructure` | Job **fails** | Job **fails** |
| Bedrock/content refusal with no `attack` | schema/`ClientError` | Job **fails** | Job **fails** |
| Target callback exception | `error` + `target` | Job **fails** unless `ignore_target_errors` | Same |
| `score == 0` | scored fail | Fails if pass-rate below threshold | Reported only |
| `score == 1` | scored pass | Counts toward pass-rate | Reported |

Optional ignore applies **only** to target exceptions, never to transport or schema errors.

## Pass rate

For each `(vulnerability, vulnerability_type)`:

```text
pass_rate = passing / (passing + failing)   # errors excluded from denominator
```

If every probe for a gated type errored, `pass_rate` is `0.0` **and** `infrastructure_errors` / `target_errors` are non-zero, so the job still fails on the error clause. That avoids “100% passed because nothing scored.”

Default `pass_rate_threshold` is `1.0` (every scored gated probe must resist). Lower it only with an explicit product decision.

Prompt leakage and excessive agency are `gated=True` in the catalog. Toxicity, illegal activity, and PII leakage are report-only until you wrap them in `CustomVulnerability(..., gated=True)` or change the catalog.

## This repository’s CI

`.github/workflows/ci.yml` runs on `pull_request` and `push` to `main`:

1. `uv sync --locked`
2. `ruff format --check`
3. `ruff check`
4. `mypy src`
5. `pytest -q`
6. `compileall`

It does **not** call staging Bedrock. It does not treat martian-agents as the red-team target. Concurrency cancels in-progress runs on the same ref.

## Customer GitHub Action

Copy `examples/github-actions.yml`. Notable choices:

- `workflow_dispatch` only in the example (start narrow; add `pull_request` when the graphs exist and quotas are understood)
- `permissions: contents: read`
- uv `0.10.0`, Ubuntu 24.04, 30 minute timeout
- Upload `results/` with `if: always()` so failed gates still leave an artifact
- No `continue-on-error`

### WAF and GitHub-hosted IPs

martian-agents public ingress uses WAF **2000 requests / 5 minutes / IP**. GitHub-hosted runners share NAT pools. Two workflows on the same IP can 429. The SDK retries 429 three times, then fails the gate as infrastructure. Keep `max_concurrent` at 2 or 1. Application-level per-key quotas are a martian-agents Phase 0/public blocker; do not treat WAF as tenancy.

## Interpreting `risk-assessment.json`

Inspect `infrastructure_errors` and `target_errors` first. A pile of `score: 1` with `errored > 0` is **not** a green run if the gate defaults are on.

For scored fails, read `reason` (evaluator prose) and `input` (what the target saw, including encodings).
