# Red team API

Every red team route is under `/v1/redteam`. The routes are frozen as version 2 in `src/martian/redteam/server/contract/redteam-api-v2.json`, an OpenAPI document.

The contract has two parts:

- **The core's routes.** `martian.redteam.server` serves them in every edition. They read the evals core's record store.
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
| `POST /v1/redteam/runs/{run_id}/findings/{finding_id}/annotations` | Add a comment, a status (`open`, `accepted`, `fixed`, `false_positive`) or both to a finding |
| `GET /v1/redteam/catalog` | The vulnerabilities with their objective class, the attack methods and the framework maps |
| `POST /v1/redteam/runs` | Start a run from a version 2 [run file](redteam-run-file.md). Answers `202` |
| `POST /v1/redteam/runs/{run_id}/stop` | Stop a run |
| `POST /v1/redteam/simulate` | Hosted: generate an attack |
| `POST /v1/redteam/evaluate` | Hosted: judge a reply |
| `GET /v1/redteam/operations/{operation_id}` | Hosted: read an operation |
| `POST /v1/redteam/operations/{operation_id}/cancel` | Hosted: cancel an operation |

Starting and stopping a run needs a launcher bound by the edition (`app.state.martian_redteam_launcher`). An installation without one answers `501`. A run file that is not valid is answered `422`, naming each problem.

## Changing the contract

`tests/redteam/test_api_contract.py` fails when a route drifts from the file. To change a route on purpose:

1. Raise `REDTEAM_API_VERSION` in `src/martian/redteam/server/contract.py`.
2. Run `uv run --extra evals python scripts/redteam_contract.py`. It refuses to rewrite the file while the version is unchanged.

When the backend changes an operation route, it raises its own version and regenerates `contracts/redteam-operations.json`. Copy that file unchanged into `contract/redteam-operations.json` and regenerate the contract.
