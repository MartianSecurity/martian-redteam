# Red team data model

`martian.redteam.model` is one set of red team objects for SDK runs, the local edition and hosted campaigns, so a run made anywhere reads the same everywhere. The terms are the hosted engine's: objective class, evidence tier, verification outcome and reproduction status. How evidence becomes a verdict is in [verdicts](redteam-verdicts.md).

## Objects

| Object | What it is |
| --- | --- |
| `RedteamRun` | One run: an SDK probe run, a local run or a hosted campaign. It holds the rest |
| `Probe` | One planned test: vulnerability, type, attack method, objective class, goal and the checks fixed before the attack |
| `Check` | A rule stated before the attack: `judge`, `text`, `canary`, `tool_call`, `receipt` or `state` |
| `Attempt` | One execution of a probe: its conversation, tool calls, check results, outcome and evidence level, and what it spent (`spend`: target queries, attacker and judge calls, tokens, cost). Its `role` is `attack`, `repetition` or `control` |
| `Turn`, `ToolCall` | A message of the conversation, and a tool call in it with its arguments and whether it ran |
| `CheckResult` | One check's result on one attempt: a hit, no hit, or undetermined |
| `Evidence` | Raw evidence captured at a trusted boundary: a reply, a tool call, a receipt, a state diff |
| `Verification` | What a probe's repetitions and negative control established |
| `Finding` | What a run reports for a probe that hit, with its level, label, status and annotations |
| `Counts` | Attempts by outcome and errors by class, and findings confirmed (level 2 and above) and suspected (level 1), never summed. `decided` is hits plus resisted, and `hit_rate` is hits over decided attempts |

An attempt's `outcome` is one of three verdicts: `hit` (the target was vulnerable), `resisted` (it held) or `undetermined` (no check could decide). An attempt that did not complete is `error`, outside the verdicts, and `error_kind` says whose failure it was: `target`, `attacker` (the model that writes attacks), `judge` (the model that judges replies) or `infrastructure` (the platform or the harness). An attempt the run's budget stopped before it was decided is `stopped`, naming the limit in `stopped_by`; it is never resisted. A hit carries its evidence level; no other outcome does. Rates are computed over decided attempts, with the undetermined and error counts shown beside them.

## Maps from today's formats

`martian.redteam.model.maps` converts the formats that exist today. Fields that have no home in the model are kept in `RedteamRun.extra`.

### Version 0.2 SDK report

`from_risk_assessment` and `to_risk_assessment` convert `risk-assessment.json` (`RiskAssessment`) both ways without losing a field.

| Report field | Model |
| --- | --- |
| `catalog_version`, `graph_revision`, `policy_revision`, `purpose`, `started_at` | The run's fields of the same names |
| `run_duration_seconds` | `RedteamRun.duration_seconds` |
| `expected_probe_count` | `RedteamRun.expected_attempts` |
| `test_cases[]` | One `Attempt` each, of the probe named by its vulnerability, type and attack method (`vulnerability:type:method`) |
| `test_cases[].score` | A `judge` check result: score 0 is a hit at level 1, score 1 resisted, none undetermined |
| `test_cases[].reason` | That check result's `rationale` |
| `test_cases[].input`, `actual_output` | `Attempt.input`, `Attempt.output` |
| `test_cases[].turns` | `Attempt.turns`; roles `human`, `ai`, `tool` become `attacker`, `target`, `tool`; each tool name a `ToolCall` |
| `test_cases[].error`, `error_kind`, error code | `Attempt.error`, `error_kind`, `error_code`, with outcome `error`. Back in this format, an `attacker` or `judge` error is an `infrastructure` error, the only other kind it has |
| `test_cases[].simulation_operation_id`, `evaluation_operation_id` | `Attempt.operations["simulate"]`, `["evaluate"]` |
| `vulnerability_type_results[].gated` | `Probe.gated` |
| `vulnerability_type_results`, `errored`, `infrastructure_errors`, `target_errors` | Computed from the test cases. A report whose own values differ keeps them in `extra` |

Each vulnerability's objective class comes from `maps.OBJECTIVES`. A custom vulnerability is a `control` objective.

### CI session rows

`from_ci_report` reads the red team report an SDK CI session records (`assert_redteam`). A row's `value` is `true` when the target resisted, `false` when the judge found it vulnerable, and absent with `error` when the probe failed. Each group's `expected` count becomes `Probe.planned_attempts`. The gate's `threshold` has no home and is kept in `extra`.

### Hosted campaign

`from_campaign` reads the engine's `CampaignSummary`, `ScoredAttempt` and `VerificationResult` objects as JSON.

| Engine field | Model |
| --- | --- |
| `probe_results[]` | A `Probe` of vulnerability `campaign` whose type is the engine's probe id. Its `coverage`, `outcome` and `reason` go in `Probe.metadata` |
| An attempt without a probe id | The probe `campaign:discovery` |
| `ScoredAttempt.oracle_results[]` (evaluated) | Check results of their tier: `text` level 2, `tool` level 3, `state` level 5 |
| `ScoredAttempt.status` | A `judge` check result: `potential_success`, `verified` and `consistent` are hits, `blocked` is not, others undetermined |
| `ScoredAttempt.transcript` | `Attempt.turns`; the first attacker message is the input and the last target message the output |
| `VerificationResult` | `Verification`, linked to the attempt of the same strategy |
| `target_calls`, `model_calls`, `input_tokens`, `output_tokens`, `mcp_calls` | `RedteamRun.budget` |
| `limitations` | `RedteamRun.limitations` |
| `iterations_completed`, `attempts`, `verified_results`, `unverifiable_results`, `strategy_branches`, `exhaustion_reason`, `authorization_gaps`, `operator_coverage` | `extra` |

## Where a run is stored

A red team run is an eval run of kind `redteam` in the evals core's record store (`martian.redteam.store`):

- each attempt is a case result, keyed by the attempt's id: `passed` when the target resisted, `failed` on a hit, and `error` when the attempt failed, was stopped by the budget or no check could decide it;
- each check result is a score of that case: `value` is whether it hit, `passed` the opposite, and its kind, level and evidence are in `metadata`;
- probes, evidence, verifications and findings are in the red team tables of the record store (migration 0015);
- the run's own red team fields are in its `provenance`.

Listings keep the kinds apart: `RecordStore.list_runs` lists eval runs unless asked for `kind="redteam"` or both. `martian redteam run` keeps each run in the local data folder (`MARTIAN_EVALS_HOME`, else `~/.martian/evals`) unless given `--no-store`. Keeping it needs the `evals` extra.
