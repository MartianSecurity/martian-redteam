# Red team API

Every red team route is under `/v1/redteam`. The routes are frozen as version 8 in `src/martian/redteam/server/contract/redteam-api-v8.json`, an OpenAPI document.

The contract has two parts:

- **The core's routes.** `martian.redteam.server` serves them. They read the evals core's record store. `martian.evals.server.create_app` mounts the router, so the local edition (`martian ui`) serves them with its Red Team pages. The hosted backend mounts it too. The app leaves the routes out of its OpenAPI document: this contract is theirs.
- **The SDK routes.** The hosted backend serves these: the four operations, the environment runs and the campaigns. `contract/redteam-operations.json` is an unchanged copy of `contracts/redteam-operations.json` in MartianSecurity/MartianBackend, which that repository generates from its own routes and checks in its own tests.

## Routes

| Method and path | What it does |
| --- | --- |
| `GET /v1/redteam/runs` | The project's red team runs, newest first. Takes `project_id` (default `default`), `limit` and `cursor` |
| `GET /v1/redteam/runs/{run_id}` | One run without its attempts and evidence: probes, verifications, findings, counts and budget |
| `GET /v1/redteam/runs/{run_id}/attempts` | The run's attempts, a page at a time (`offset`, `limit`), optionally of one `probe_id` |
| `GET /v1/redteam/runs/{run_id}/attempts/{attempt_id}` | One attempt with its conversation, tool calls and check results |
| `GET /v1/redteam/runs/{run_id}/evidence/{evidence_id}` | One piece of evidence |
| `GET /v1/redteam/runs/{run_id}/findings` | The run's findings |
| `GET /v1/redteam/runs/{run_id}/report` | The run's [report standard](redteam-reports.md): what attacked, the budget, success rates with intervals, refusals, findings by level, verification and controls |
| `POST /v1/redteam/runs/{run_id}/findings/{finding_id}/annotations` | Add a comment, a status (`open`, `accepted`, `fixed`, `false_positive`) or both to a finding |
| `GET /v1/redteam/catalog` | The catalogue: each vulnerability with its group, objective class, default severity, types, forms (their checks, the evidence level a hit reaches and what it proves), framework tags, source and licence; the attack methods; and the framework maps ([catalog](catalog.md)) |
| `POST /v1/redteam/runs` | Start a run from a version 2 [run file](redteam-run-file.md). Answers `202` |
| `POST /v1/redteam/runs/{run_id}/stop` | Stop a run |
| `POST /v1/redteam/simulate` | Hosted: generate an attack. With `source: reviewed_cases`, the server writes it from one reviewed case and one attack skill and answers the attack alone; an edition without reviewed cases is refused with `403` |
| `POST /v1/redteam/evaluate` | Hosted: judge a reply |
| `GET /v1/redteam/operations/{operation_id}` | Hosted: read an operation |
| `POST /v1/redteam/operations/{operation_id}/cancel` | Hosted: cancel an operation |
| `POST /v1/redteam/environment-runs` | Hosted: admit a run in a Martian environment for an approved agent; answers the session for the agent's tools and a run key ([environments](redteam-run-file.md#environments)) |
| `POST /v1/redteam/campaigns` | Hosted: start the console's planned campaign for an approved agent at a depth, as the member the API key belongs to ([campaigns](redteam-reports.md#a-planned-campaign-on-a-martian-server)) |
| `GET /v1/redteam/campaigns/{run_id}` | Hosted: a campaign's state, release decision and, once it finished, its run in the shared model |
| `POST /v1/redteam/environment-runs/{run_id}/world` | Hosted, with the run key: `prepare`, `observe` or `outcome` of one attempt's world: reset it, then read its state checks and the gateway's calls of the attempt |
| `POST /v1/redteam/environment-runs/{run_id}/finish` | Hosted, with the run key: end the run, releasing its session and instances |

Starting and stopping a run needs a launcher bound by the edition (`app.state.martian_redteam_launcher`). An installation without one answers `501`. The local edition (`martian ui`) binds `LocalLauncher`, reading a run file's relative `file` from the folder `martian ui` was started in. `martian.redteam.engine.launcher.LocalLauncher` runs the engine in the server's process: `start` saves the run as `running` and answers at once, the run is kept after each attempt, and `stop` cancels it and keeps it as `cancelled`. The service is chosen, and `${NAME}` in the target's address and headers is read, from the server's environment, as `martian redteam run` reads its own. A run file it cannot run (a target kind this release cannot call, no service, a judge check without a judge model, a `${NAME}` the environment does not set) is answered `422`, and a `request_id` a run already has `409`. A run file that is not valid is answered `422`, naming each problem. A run file that names an `environment` needs the edition's `redteam_environments` feature key ([editions](evals-core.md#editions-and-entitlements)); without it the route answers `403` before any launcher runs, for example "The Community edition does not include red team environments".

## The local edition's routes

`martian ui` also serves two routes of its own, outside the contract (`martian.redteam.server.local`). The Red Team UI's setup page and run report use them:

| Method and path | What it does |
| --- | --- |
| `POST /v1/local/redteam/runs` | Start a run as `POST /v1/redteam/runs` does, with the same body and answers, plus an optional `target_id`: one of the local edition's [targets](evals-core.md#local-targets), shared with Evals |
| `POST /v1/local/redteam/runs/{run_id}/resume` | Go on with a stopped, cancelled or failed run with the run file and target it was started with. Takes `project_id`. Answers `202` with the run, `running` again |

With `target_id`, the run file must reach the target at the target's own address (`target.url`; for a model target, its `/chat/completions` address), or the route answers `422`, so the key goes nowhere else. The run file names the key as `${MARTIAN_TARGET_KEY}`, and the launcher fills that one name in with the target's key when it builds the target, in the server's process. A target without a key leaves it unset, whatever the server's environment holds. The same file runs from the command line with `MARTIAN_TARGET_KEY` set, and plans the same probes, with the same attempt fingerprints.

Every run the launcher starts keeps the original run file and target ID under `provenance.launch`
in the local store and makes relative target file paths absolute. Use environment references for
keys in the run file. The launcher uses this configuration for resume. Reports and pushes omit it,
and the launcher refuses to resume client-executed runs received by push. Each finished attempt
whose fingerprint is unchanged is kept. Cumulative recorded spend still counts against the budget.
A run that is still `running` in another session, already completed, or lacks local launch
configuration returns `409`. If `martian ui` stopped while a run was running, `stop` records
`cancelled` and preserves the warning about incomplete spend. A spend-capped resume whose earlier
accounting is [unverifiable](redteam-run-file.md#budget) returns `409` and preserves the saved run and
its evidence. A re-judged run also returns `409`. Resume with the original execution run ID.

The launcher and CLI hold one session per stored run until work ends. A new resume or a stop
request from another process returns `409` while that session is active.

### Local findings and remediation

These routes are mounted by `martian ui` under `/v1/local/redteam`, outside the shared contract.
All take `project_id`, default `default`.

| Method and path | Response |
| --- | --- |
| `GET /findings` | Grouped findings, total matching count, project counts, filter choices and `export_formats`. Accepts `q`, `target`, `severity`, `status`, `category`, `vulnerability_type`, `evidence`, `run_id`, `seen_after_fix`, `sort` (`severity` or `recent`), `offset` and `limit` (1–200) |
| `GET /findings/{finding_id}` | A finding, its scan observations and review history. Unknown ID or wrong project returns `404` |
| `POST /findings/{finding_id}/triage` | Appends a record and returns `201`. Accepts a `comment`, `status` (`open`, `accepted`, `fixed`, `false_positive` or `ignored`) and/or `severity`. Omit severity to keep it or send `null` to restore scan severity. Requires `review`; an empty change returns `422` |
| `GET /findings/export/{format}` | An attachment using the list filters across all pages. Takes `csv`, `sarif`, `colang-v1` or `colang-v2`. The Colang formats return `application/zip` with a manifest and the configuration for any mapped message policies |
| `GET /runs/{run_id}/report.pdf` | A `redteam-report.pdf` attachment with recorded coverage, findings and remediation. An unknown run or wrong project returns `404`. A queued or running scan returns `409` |
| `GET /runs/{run_id}/remediation` | The scan's executive summary, findings, evidence references, ranked actions and `prompt_controls` as JSON. Each suggested control contains `id`, `title`, `instruction` and the confirmed `finding_ids` supporting it |
| `GET /runs/{run_id}/remediation/print` | The same plan as printable HTML |
| `GET /runs/{run_id}/remediation/follow-ups` | Later finished scans with a matching recorded target, newest start first. Returns `items` with ID, purpose, state and timestamps, `has_more`, and an `unavailable_reason` for a baseline that cannot identify follow-ups. `limit` defaults to 20 and accepts 1–100 |
| `GET /runs/{run_id}/remediation/verification` | Requires `candidate_run_id`. Compares saved scans of the same target. Returns `baseline_run`, `candidate_run`, per-finding verification in `findings`, `baseline_summary`, `candidate_summary`, `coverage`, and `follow_up_findings` with new findings and escalations |
| `POST /runs/{run_id}/remediation/verified-fix` | Requires review access. The body takes `candidate_run_id`, the baseline scan's `finding_id`, and a UUID `request_id`. Returns the inventory finding ID and the evidence-linked triage note with status 201. Reuse the request ID to recover the same decision. Insufficient coverage, newer observations, or conflicting request IDs return 409 |

The remediation routes return `404` for a missing scan and `409` while it is queued or
running. Verification also returns `409` when the pair cannot establish a follow-up on the
same recorded target. See [local findings and remediation](redteam-findings.md) for grouping, review and
evidence semantics.

## Changing the contract

`tests/redteam/test_api_contract.py` fails when a route drifts from the file. To change a route on purpose:

1. Raise `REDTEAM_API_VERSION` in `src/martian/redteam/server/contract.py`.
2. Run `uv run --extra evals python scripts/redteam_contract.py`. It refuses to rewrite the file while the version is unchanged.

When the backend changes an operation route, it raises its own version and regenerates `contracts/redteam-operations.json`. Copy that file unchanged into `contract/redteam-operations.json` and regenerate the contract.

## Checking a red team service

The engine reaches attack generation and judging only through `RedteamService` (`simulate` and `evaluate`), which the local service and the hosted client both implement. `martian.redteam.engine.conformance` is the suite every implementation must pass: it calls `simulate` for every catalogue vulnerability and type, each LLM attack method, both modes, a 4096-character purpose and a request of the caller's own, and `evaluate` with every attack method, custom criteria, an empty reply and the tools called. It checks that each output has an attack and a method, or a score that is exactly the integer 0 or 1 and a reason, and, given a service whose model calls fail, that every failure is a `ClientError` naming `attacker` or `judge` when it names a model.

Check an implementation with one command, naming a callable that returns it (and, optionally, one that returns a failing one):

```bash
uv run --extra evals python -m martian.redteam.engine.conformance my_package.module:make_service --failing my_package.module:make_failing_service
```

It prints each check and exits 1 when one fails. `tests/redteam/test_conformance.py` runs it in CI on the local service, against a loopback model server, and on the hosted client, replaying operation receipts in the shapes of `contract/redteam-operations.json`.

## Pushed runs

`martian.redteam.server.push` serves `PUT /v1/push/redteam/runs/{run_id}`, beside the evals push routes and outside the contract, as they are. The body is `{"run": ...}`, a finished run of the shared model. The answer is `{"run_id", "project_id", "created"}`. The route needs the `run` capability and writes as the edition's push principal: `martian ui` serves it to its single user, and the hosted backend binds organisation API keys with `redteam:run` to it. The run is kept with `provenance.execution` set to `client_executed`. The same run again answers `created: false` and writes nothing; other content under the same id is `409`; a run that has not finished, or that names another id than the path, is `422`. `martian redteam push` sends runs here ([pushing a run](redteam-reports.md#pushing-a-run-to-a-martian-server)).
