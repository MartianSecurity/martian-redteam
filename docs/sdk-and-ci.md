# Unified SDK and CI

`martian-sdk` is the canonical Python 3.12+ distribution. It contains
`martian.evals`, `martian.redteam`, the `martian` CLI, and the legacy import
names. The source repository remains `MartianSecurity/martian-redteam`. The package
is published on [PyPI](https://pypi.org/project/martian-sdk/). Install an exact
released version with its `evals` extra for the persisted CI runner:

```bash
python -m pip install 'martian-sdk[evals]==<version>'
martian --version
```

Replace `<version>` with a release listed on PyPI.

`evals` includes MLflow-skinny **3.18.0** and pytest. The SDK never connects to
the hosted MLflow server, so this pin is independent of the server's version,
which MartianSecurity/martian-evals sets. The base installation supports redteam
callbacks and local reports. The SDK reaches MLflow internals only through
`src/martian/evals/_mlflow.py`, which lists each one. Before moving the pin, run
the [MLflow compatibility suite](development.md#mlflow-upgrades) on the
candidate release.
The separate `mcp` extra adds the existing customer MCP integration.
The `postgres` extra adds the Postgres driver for the in-process evals store on Postgres
(see [evals core](evals-core.md#the-in-process-binding)).

A newer MLflow migrates a local MLflow database in place the first time it opens
one: a suite run's `mlflow.db`, the pytest kit's store when
`MARTIAN_EVALS_STORE_DIR` keeps it, or an `MlflowStore` database. An older SDK then refuses that database as out of date, so back up a
store you keep before upgrading the SDK.

## Python interfaces

```python
from pathlib import Path
from martian import MartianConfig, CiContext
from martian.ci import CiSuite, run
from martian.evals import EvalsConfig, evaluate, load_dataset, exact_match, assert_gate, assert_checks, assert_redteam
from martian.redteam import Client, PromptLeakage, PromptInjection, a_red_team

result = run(CiSuite(
    evals_paths=(Path("tests/evals"),),
    redteam_config=Path("redteam.yaml"),
    output_dir=Path("results"),
    require_export=True,
))
if not result.passed:
    raise SystemExit(result.exit_code)
```

Omit either suite to run the other independently. `run` returns a typed
`SessionResult` with the session ID, aggregate verdict, individual test
provenance, upload/export state and problems. `result.save(path)` writes a safe,
versioned JSON receipt. Call the runner serially within a process: pytest and
MLflow have process-global state. It refuses an active MLflow run and restores
its temporary tracking/scorer configuration afterward.

`evaluate` delegates to MLflow's data/prediction/scorer API and returns its
`EvaluationResult`. Write `@mlflow.test` regression tests and call `assert_gate`
for a pinned Backend dataset or `assert_checks` for inline scorer results. See
[the eval guide](evals.md) and [the dataset example](../examples/evals/test_support_agent.py).
Prediction functions and redteam target callbacks execute in the customer
process; sync and async callbacks remain supported. Backend holds no target
provider key.

## One customer connection

Both modules use the same immutable `MartianConfig`. It contains one approved
Backend origin, a `SecretStr` token, `request_timeout_seconds=120` (1–240), and
`trust_env=False`. CI attribution is a separate immutable `CiContext`.

```python
from martian import MartianConfig, CiContext
from martian.redteam import Client, a_red_team
from martian.evals import load_dataset

config = MartianConfig(api_url=approved_backend_origin, token=api_token)
# Standalone calls require no agent ID or Git revision.
dataset = load_dataset("support", config=config)
async with Client(config=config) as client:
    assessment = await a_red_team(..., client=client)
# Persisted CI additionally attributes work to an agent and tested revision.
result = run(suite, config=config, ci=CiContext(agent_id=agent_id, git_sha=tested_sha))
```

`Client(api_url=..., token=...)` is equivalent; supply the complete pair.
`Client(config=...)` cannot be mixed with separate connection arguments.
Without explicit config, only these canonical process variables are read:

| Setting | Purpose |
| --- | --- |
| `MARTIAN_API_URL` | Approved Backend origin, without a path |
| `MARTIAN_API_TOKEN` | Organisation API key with required capabilities |
| `MARTIAN_AGENT_ID` | Agent UUID, required for persisted CI |
| `MARTIAN_EVALS_GIT_SHA` | Tested commit; otherwise GitHub/local Git metadata |

Create one API key under the console's **API Keys** settings (admin/owner),
select only the needed capabilities and applicable restrictions, and save the
once-shown token in your secret manager. Evals dataset reads need `evals:read`;
redteam needs `redteam:run`; uploads/sessions need their evals/CI capabilities.
The key neither grants model entitlement nor administers the console. An
agent-bound key supplies its immutable agent binding automatically; explicit
`Client(agent_id=...)` attribution must match that grant. This labels/access-binds
the operation and does not attest the local callback identity.
Existing evals CI tokens keep their original evals/CI grants; create a new key
with `redteam:run` to add generation/judging. Revocation stops subsequent calls.
Hosted features require the SDK APIs to be enabled on the Backend and Agents
services for the organisation; an SDK install does not enable a hosted feature.

`EvalsConfig(api_url=..., token=..., agent_id=..., git_sha=...)` remains an
adapter for existing evals options/attribution. It can be passed to `run`,
which rejects conflicting explicit `ci=` metadata. Platform dataset reads use
`MartianConfig` directly and require no CI metadata. No implicit `.env` is read.
Unused legacy variables are ignored, including invalid values. YAML `api.url`
is rejected in modern mode before callback import. Redirects are disabled;
environment proxies are disabled unless `trust_env=True` is explicitly chosen.
HTTPS is required except loopback development. Target/provider credentials
belong to the local callback and remain separate from Martian authentication.

## CLI and one-session behavior

```bash
# Independently persisted suites:
martian ci --evals-path tests/evals --output results
martian ci --redteam-config redteam.yaml --output results
# One combined persisted session:
martian ci --evals-path tests/evals --redteam-config redteam.yaml --require-export --output results
# A suite file, run locally with only a model key (see the eval guide):
martian evals run evals.yaml
# The Evals UI on this machine, one user and no login (see the evals core guide):
martian ui
# Inspect AI logs as runs in that UI (see the evals core guide):
martian evals import inspect logs/*.eval
# promptfoo results as runs in that UI, and a promptfoo config as suite files (see the evals core guide):
martian evals import promptfoo results.json
martian evals convert promptfoo promptfooconfig.yaml --target agent.py:answer --output suite
# An MLflow tracking store copied into that UI, after a schema version check:
martian evals import mlflow ./mlflow.db
# All of that UI's data exported, resumably, and loaded into a fresh data folder (see data export):
martian evals export ./evals-export
martian evals import martian ./evals-export --data-dir ./new-data
# Additional existing interfaces:
martian evals run tests/evals -q
martian redteam validate redteam.yaml
martian redteam run redteam.yaml -o results/redteam
```

Python and `martian ci` use one runner and one pytest execution. The YAML suite
is a pytest item in that execution. Session finalization uploads dataset runs
and then **one** `POST /v1/evals/ci/sessions` containing dataset, checks and/or
redteam records. The CLI no longer combines two subprocess exit codes.

`martian redteam run` uses Backend generation/judging and writes a local gate/report;
`martian ci --redteam-config` also persists it through Backend. `martian evals
run` forwards pytest options and supports eval-only xdist. The combined runner
is serial. Do not also include the same redteam assessment as a pytest test.

Success requires every required test/probe to complete and meet its policy,
and a complete, consistent Backend session acknowledgment.
Synchronous callbacks run off the event loop so deadlines can fire. Their own
network timeouts are still required: cancellation cannot kill a Python thread,
and the SDK awaits worker cleanup before returning. Sync workers remain capped
at the configured concurrency even after a timeout. Late replies never become passing judgments. Empty/missing
gates, deselection, skipped coverage, setup errors, interrupted graphs,
missing/invalid scores, target timeouts, execution errors and failed required
uploads fail the job. A caught gate exception remains recorded as a failure.
Backend dataset results are retained even when an additional local scorer fails;
a failed runner record accompanies the Backend-scored run.

The session verdict is `failed` if any required test failed, `relaxed` if all
passed but one required a lower declared threshold, otherwise `passed`.
Relaxed outcomes are displayed as such. Lower score thresholds never excuse
missing scores or execution errors. The declared revision and uploaded results
are runner assertions, not binary attestation or release authorization.

`--require-export` waits at most 30 seconds for session export readback and
fails if a configured required export does not complete. Polling never runs a
model or retries an assessment. Backend uploads have stable request IDs and
bounded retries for transport errors, admission 429 and transient gateway
statuses (at most three upload attempts). Redteam execution POSTs are sent once.
Transport ambiguity or a gateway 5xx permits only GET recovery of the same UUID.
Polling and HTTP phases share one absolute local wait deadline; a timeout
returns `RedteamOperationError.operation_id` for `client.get_operation(id)`
without another model call. For resumable jobs, persist your own UUID before
calling `client.simulate(context, operation_id=id)` or
`client.evaluate(context, attack=..., target_output=..., operation_id=id)`.
Reusing an ID cannot dispatch a second operation; a changed payload conflicts.
`client.cancel_operation(id)` requests cancellation.
Failed/cancelled/uncertain/purged operations and expired/missing results fail
closed; score 0 is a completed failing judgment and is never retried.

The receipt `results/ci-session.json` is written before uploads and updated
atomically afterward. YAML runs additionally retain sanitized completed probes
in `results/redteam/probes.jsonl`, plus JSON/Markdown reports. A process killed
mid-run leaves a running/incomplete receipt with a nonzero default exit code;
it is not a successful upload. A hard kill cannot upload to Backend.

## Shared pytest sessions and result provenance

`assert_redteam(assessment, threshold=1.0)` records saved probes without target
or judge re-execution. `--martian-require-redteam` requires that gate in a
pytest session. The YAML runner determines expected probe counts from the
catalog/configuration before execution and supplies them to the adapter.
When adapting an externally saved assessment, pass `expected={(vulnerability,
type): count}` if the original plan is available; a saved report cannot attest
that unreported work ran.

Redteam uses an explicit `kind: "redteam"` contract containing catalog/graph
and server policy revisions, operation IDs, threshold, required/informational groups with expected counts, and
**every** probe's judgment, evidence digest and error. Informational failures
stay visible; execution errors and missing informational probes still make the
suite incomplete. Limits are 100 tests, 1,000 combined rows, and the Backend's
4 MiB request ceiling. Oversized suites fail; results are not silently dropped.

Backend computes the redteam aggregate from these rows and declared policy.
Judgments are `client_executed_client_scored`, including judgments originally
obtained from Agents. Inline scorer checks have the same provenance. Pinned
dataset outputs remain `client_executed_server_scored`: Backend computes the
canonical exact-match score. The console shows all kinds under Evals → Runs,
including policy and provenance, and refresh reads the persisted record.

Checks/redteam tests use Backend's journaled private MLflow export: one run per
test, session/commit/provenance tags, counts/status and `results.json` containing
the preserved judgments and policy. Dataset tests export their own EvalRun.
SDK clients never receive the organisation's MLflow credentials.

## Safe reports

Reports default to digests of target inputs, outputs and rationales. Set
`MARTIAN_REPORT_INCLUDE_CONTENT=1` or explicitly call
`assessment.save(path, include_target_content=True)` only for approved data.
`MARTIAN_EVALS_REDACT_INPUTS=1` and `MARTIAN_EVALS_REDACT_OUTPUTS=1` take
precedence, including rationales. Known environment and constructor credential values are redacted even
with content enabled. A private redaction policy survives assessment/receipt
copies and saves after client close; it never enters JSON or repr. Raw
in-memory target data/model dumps remain customer-owned data, not safe reports. Exception messages from targets/remote validation bodies
are not copied into reports. Safe receipts contain identities/counts/status,
not target bodies. Do not upload the private local MLflow SQLite store/traces
as a public CI artifact: it contains prediction inputs, outputs and expectations.
Backend still needs raw dataset outputs to score them, and Agents judging
receives attack/reply text. See [the data boundary](security-and-data.md).

## Reporting failures

Expected API failures show a short explanation, an error code, and an operation
ID when execution has an identity. Authentication, scope, request limits, expired
results, and unknown execution outcomes have distinct guidance. For example:

```text
Permission denied. Check the key's capabilities, agent scope, and organisation access.
[http_403; operation_id=11111111-1111-4111-8111-111111111111]
```

Python callers can inspect `RedteamOperationError.code`, `operation_id`, and the
optional `reason_code` / UUID `request_id`. The existing `code` values remain
compatible. An unknown execution outcome still requires `get_operation` with
the same ID; the SDK does not replay the execution POST to obtain a pass.
Eval upload failures retain their UUID request ID when one is available.

Default redteam reports retain safe codes and existing operation-ID fields;
`case.error_code` also reads the code from a reopened report. The JSON shape is
unchanged, so earlier SDK versions can still load these reports. Raw provider
bodies and callback exception text remain outside safe reports.

The CLI groups repeated errors and limits terminal detail. Full safe per-probe
references remain in `risk-assessment.json`; CI problems remain in
`ci-session.json`. Setup failures exit with status 2 and show a local report ID
matching `execution-error.json`, which includes the SDK version and controlled
message. Gate failures continue to exit with status 1. Expected CLI failures do
not print a traceback. Error receipts are local files, not automatic uploads.

To report a problem, include the SDK version, error code, relevant operation or
request ID, and the safe receipt (or workflow run link). A CLI setup report ID
identifies its local receipt; it is not a Backend request ID. Successful commands
do not emit extra diagnostics. Applications using the Python SDK retain control
of their own logging and exception handling.

## Migration, release and qualification

Replace `pip install martian-redteam` or `martian-evals-kit` with
`martian-sdk[evals]`, and replace imports with `martian.redteam` or
`martian.evals`. Legacy evals namespaces remain adapters over the same implementation.
`martian_redteam.Client` and `martian-redteam run` deliberately retain direct
Agents access only for explicit operator integrations. They require either
`Settings(api_url=..., api_key=...)` with a complete pair or the dedicated
`MARTIAN_REDTEAM_API_URL` / `MARTIAN_REDTEAM_API_KEY` environment pair. They
ignore generic `MARTIAN_API_*` and `.env`; YAML cannot change that origin.
Old modern `Client(api_url=..., api_key=...)` and `run(redteam_settings=...)`
raise controlled migration errors, without auth fallback or token detection.
The single-key claim applies to evals/redteam platform APIs; optional runtime
telemetry, call-gate, target-provider and MCP credentials have separate scopes. The old `martian-evals/ci` project becomes
a metadata-only compatibility package depending on this SDK. Do not install
old, code-bearing distributions alongside the new wheel: uninstall them first,
or create a fresh environment. Load only one pytest plugin path.

The canonical version is `src/martian/_version.py`; packaging, both modules and
legacy imports use it. [Release preparation](releasing.md) covers wheel/sdist,
clean installs and PyPI Trusted Publishing. [Architecture](architecture.md)
documents execution, scoring and trust boundaries. Fast contract tests use
loopback fixtures and require no paid model. [Validation](validation.md)
separates those tests from actual running-service/model qualification.
