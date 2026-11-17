# Red team API

Every red team route is under `/v1/redteam`. The routes are frozen as version 3 in `src/martian/redteam/server/contract/redteam-api-v3.json`, an OpenAPI document.

The contract has two parts:

- **The core's routes.** `martian.redteam.server` serves them. They read the evals core's record store. `martian.evals.server.create_app` mounts the router, so the local edition (`martian ui`) serves them with its Red Team pages. The hosted backend mounts the evals routers on its own and does not mount this one yet (MartianSecurity/martian-notes#299). The app leaves the routes out of its OpenAPI document: this contract is theirs.
- **The SDK operation routes.** The hosted backend serves these four routes. `contract/redteam-operations.json` is an unchanged copy of `contracts/redteam-operations.json` in MartianSecurity/MartianBackend, which that repository generates from its own routes and checks in its own tests.

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
| `GET /v1/redteam/catalog` | The vulnerabilities with their objective class, the attack methods and the framework maps |
| `POST /v1/redteam/runs` | Start a run from a version 2 [run file](redteam-run-file.md). Answers `202` |
| `POST /v1/redteam/runs/{run_id}/stop` | Stop a run |
| `POST /v1/redteam/simulate` | Hosted: generate an attack |
| `POST /v1/redteam/evaluate` | Hosted: judge a reply |
| `GET /v1/redteam/operations/{operation_id}` | Hosted: read an operation |
| `POST /v1/redteam/operations/{operation_id}/cancel` | Hosted: cancel an operation |

Starting and stopping a run needs a launcher bound by the edition (`app.state.martian_redteam_launcher`). An installation without one answers `501`. The local edition (`martian ui`) binds `LocalLauncher`, reading a run file's relative `file` from the folder `martian ui` was started in. `martian.redteam.engine.launcher.LocalLauncher` runs the engine in the server's process: `start` saves the run as `running` and answers at once, the run is kept after each attempt, and `stop` cancels it and keeps it as `cancelled`. A run file it cannot run (a target kind this release cannot call, no service, a judge check without a judge model) is answered `422`, and a `request_id` a run already has `409`. A run file that is not valid is answered `422`, naming each problem. A run file that names an `environment` needs the edition's `redteam_environments` feature key ([editions](evals-core.md#editions-and-entitlements)); without it the route answers `403` before any launcher runs, for example "The Community edition does not include red team environments".

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
