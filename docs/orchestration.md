# Orchestration

The Phase 1 loop is **simulate → encode → target → evaluate**, once per (vulnerability, type, attack, repeat) tuple.

## Entry points

| Function | Use |
| --- | --- |
| `a_red_team(...)` | Async callers. Requires an already-open `Client`. |
| `red_team(...)` | Sync wrapper via `asyncio.run`. Raises if an event loop is already running; use `a_red_team` then. |
| `martian redteam run config.yaml` | Loads YAML, opens `Client` in one loop, writes the JSON and Markdown reports, applies the gate. |

`Client` is an async context manager. The CLI uses a single `asyncio.run` so `aclose()` happens on the same loop that ran the probes. Do not create a `Client`, call `red_team()`, then `asyncio.run(client.aclose())` — that second loop will fail.

## Job expansion

```text
for vulnerability in vulnerabilities:
  for type in vulnerability.types or allowed_types:
    for attack in attacks:
      repeat attacks_per_vulnerability_type times:
        enqueue probe
```

Defaults: `max_concurrent=2`, `attacks_per_vulnerability_type=1`, `target_timeout_seconds=120`.

Empty vulnerability or attack lists, empty `purpose`, `max_concurrent < 1`, or `target_timeout_seconds <= 0` raise `ValueError` before any HTTP.

Example: `PromptLeakage(types=["system_prompt"])` + `[PromptInjection(), ROT13()]` → **2 probes**.

`PromptLeakage()` with no type filter uses all allowed types (`system_prompt`, `instructions`, `credentials`) × each attack.

Attack `weight` is stored on the catalog object for later sampling. Phase 1 does **not** sample; every listed attack runs. Weights must be `> 0`.

## One probe

1. **Simulate.** `Client.simulate(SimulatorContext(...))`.
   - LLM attacks send `attack_method=<attack.id>` (today: `prompt_injection`).
   - Encoding attacks send `attack_method=prompt_injection` so the graph still generates natural language, then the SDK encodes.
   - `mode=baseline`.
2. **Encode** if `attack.kind == "encoding"` (`rot13`, `base64`, `leetspeak`). Pure local transforms. Unknown methods raise `ValueError` (should be unreachable if the catalog is used).
3. **Target.** `model_callback(payload)` in the customer process. Sync or async, `str` or `Turn`. Exceptions become `error_kind=target`. Each call has a deadline of `target_timeout_seconds` (default 120); an await that exceeds it is cancelled and recorded as `error_kind=target` (`target callback timed out after N seconds`). The generated attack is still stored on `TestCase.input` so reports remain useful.
4. **Evaluate.** `Client.evaluate(...)` with the **encoded** payload (what the target actually saw), the reply, and `tools_called`. Custom vulns include `criteria`.

`asyncio.gather` + `Semaphore(max_concurrent)` runs probes. Each `_probe` turns `ClientError` from the hosted calls (`error_kind=infrastructure`) and exceptions or timeouts from the callback (`error_kind=target`) into the `TestCase` so one failure does not cancel siblings. The **gate** then fails the process. Any other exception is an SDK defect and propagates out of `a_red_team` rather than being blamed on the target.

## Callback adapter

`wrap_callback` inspects the callable’s signature once:

- Parameter named `turns` → called as `callback(prompt, turns)`.
- Otherwise → `callback(prompt)`.
- Async callables run on the event loop; synchronous callables run in worker threads so they cannot prevent deadlines or sibling probes from progressing.
- Await if the result is awaitable.
- `str` → `Turn(role="ai", content=str)`.
- `Turn` → returned as-is.
- Anything else → `TypeError`, recorded as a target error if it happens inside `_probe`.

Synchronous callbacks need their own network/I/O timeouts. Cancelling an await
cannot terminate a running Python thread. The SDK awaits owned worker cleanup
off the event loop before returning, so another CI session cannot switch its
tracking/tenant context while callbacks are still active. Each assessment caps sync workers at
`max_concurrent`, including after a timeout, and cancels queued work on exit. Late replies are discarded and never judged as a pass.
Use async callbacks for cooperative cancellation and make sync callbacks safe
for the configured concurrency (or select `max_concurrent=1`).

Do not call Martian from the callback. The callback **is** the system under test (or a wrapper around it).

## Concurrency and rate limits

Default two in-flight probes, using typed Backend operations in modern mode. Raise only after measuring latency and admission headroom.

Backend enforces per-key, organisation and global concurrency and admitted-stage
budgets before native execution. Network/proxy limits may additionally reject
requests. Keep `max_concurrent` at 2 (or 1) unless the granted limits support
more. A 429 is an infrastructure error, not skipped coverage; modern execution
POSTs are not automatically replayed.

## Reporting

`RiskAssessment` includes:

- `catalog_version`, graph/policy revisions and simulation/evaluation operation IDs
- `purpose`, `started_at`, `run_duration_seconds`
- `test_cases[]` with input, output, score/reason **or** error/`error_kind`
- `vulnerability_type_results[]` with pass/fail/error counts and `gated`
- `errored`, `infrastructure_errors`, `target_errors`

Pass rate for a type is `passing / (passing + failing)`. Error-only types have pass rate `0.0` and are still caught by the infrastructure/target error gates.

`assessment.save(directory)` writes `directory/risk-assessment.json` and `directory/risk-assessment.md` (creating the directory if needed) and appends the Markdown report to `$GITHUB_STEP_SUMMARY` when set. The CLI prints a Rich overview table plus a table of failed or errored probes.

## What Phase 1 does not do

- Multi-turn conversations (`turns` is always `None`)
- Simulator `mode=enhance`
- Mid-loop metric checks
- Direct uploads from this probe loop (the separate CI session finalizer uploads results to Backend)
- Telemetry
- Invoking the target from Martian’s VPC
