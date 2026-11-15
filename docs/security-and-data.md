# Security and data

## Trust boundary

```text
Customer process             Approved Backend origin          Agents
local target callback <--- SDK -- Bearer / typed DTOs --------> execution owner
         |                    |                                  |
         +-- target provider  +-- eval outputs / CI records       +-- models
```

Modern SDK clients call only Backend with one scoped platform token. The target
callback remains local and owns its provider authentication. Simulation sends
catalog IDs, purpose, method/mode and an optional prompt. Judging sends attack,
reply, tool identifiers and optional custom criteria. These texts can contain
sensitive target data; they cross the customer boundary and enter the service's
controlled operation/recovery lifecycle. SDK digests do not remove data from
actual model requests. Backend needs raw dataset outputs for canonical scoring.

Requests cannot choose model/service/target endpoints. YAML cannot override the
platform origin. HTTPS is required except loopback; redirects and environment
proxies are off unless proxies are explicitly opted in. Constructor connections
must supply a complete URL/token pair. No `.env` is loaded and unused legacy
variables are ignored; legacy auth is never selected after a failed public call.

Reports default to content digests. Content opt-in does not disable credential
redaction, including constructor secrets after client close and report copies.
Explicit input/output redaction flags take precedence. Raw in-memory assessment
fields/model dumps and private local MLflow traces are not safe public reports.

## Local suite runs

`martian evals run evals.yaml` does not use the platform connection above and sends nothing to
Martian. Its outbound calls are the ones the suite file names:

- **A URL target** (`target.url`) receives one JSON `POST` per case, or per attempt when the
  suite repeats its cases, carrying that case's input.
  The suite file chooses this address, so review a suite file from someone else before running
  it with your credentials loaded. `http` is accepted as well as `https`; use `https` for any
  endpoint that is not on your own machine or network.
- **Header values** can reference `${NAME}`, read from the environment when the suite loads.
  Only the names appear in the suite file. The values are sent to the target and are not
  written to `results.jsonl`, `summary.json`, `junit.xml`, the MLflow trace or the local Evals
  UI's data folder. They are not
  expanded in the request body, and text inside a case is never expanded. Loading a suite file
  into the [shared eval model](eval-model.md#the-sdk-suite-file), as a store keeps it, keeps the
  URL and header values as written, with `${NAME}` unexpanded, so the environment's values are
  never stored. A literal value is stored as it is: keep credentials in the environment.
- **An LLM judge** receives the case's input, the reply and your criteria at the provider you
  configure.
- **MLflow** receives inputs, replies and judge reasons: a local store by default, or the
  server named in `mlflow.tracking_uri`.

Every run is also recorded in the local Evals UI's data folder on this machine (`--data-dir`,
`MARTIAN_EVALS_HOME` or `~/.martian/evals`), which `martian ui` reads: each case's inputs, reply,
verdicts and reasons as `results.jsonl` holds them (every attempt's, when the suite repeats
cases), its trace with the target's spans, and the
target as the suite file writes it. Nothing there leaves the machine; treat the folder as you
treat `results.jsonl`.

Redirects are not followed and proxy settings from the environment are not used for the target
call. Target errors name the failure, never the address, headers or response body.

`martian evals import promptfoo` records a promptfoo results file in the same folder: each row's
prompt, vars, output and assertion reasons. It does not copy the file's `config` section, which
promptfoo writes with best-effort redaction of provider settings; the run keeps only the config's
`description` and the file's path and hash. An assertion's grading `provider` given as an
object, with settings that may hold a key, is kept as its id only. `martian evals convert
promptfoo` writes suite files and calls nothing: a converted suite calls only the `--target`
function it names, never the config's providers.

`martian evals import mlflow` copies an MLflow tracking store into the same folder. It opens the
store so that it cannot write it (SQLite read-only, Postgres with read-only transactions) and never
migrates it, and reads the trace files the store names on this machine only. A Postgres URL's
password is not printed or stored: the report and each copied run's provenance keep the URL
without it. The folder then holds a copy of the store's traces, inputs, outputs and assessments.
See [copied MLflow stores](evals-core.md#copied-mlflow-stores).

`martian evals export` writes everything the data folder's store and record store hold into the
folder it names: every case's inputs and outputs, traces with their spans, scores, reasons and
reviews, and run artifacts. It holds no credentials: the local targets and their keys
(`targets.json`, `target-keys.json`) and `settings.json` are not exported, and a target's
`${NAME}` references stay unexpanded, as the store keeps them. Treat an export as you treat the
data folder. See [data export](data-export.md).

## Telemetry

The evals processes send no telemetry, and neither do the libraries they bundle: `martian ui`,
`martian evals run`, the eval gate, a server that serves the [evals core](evals-core.md) and
each of its worker processes. Importing `martian.evals` sets `MLFLOW_DISABLE_TELEMETRY=true`
before it imports MLflow, unless the environment already sets the variable, and each worker
process inherits it. MLflow decides when it is first imported whether to start its telemetry
client, which otherwise asks `config.mlflow-telemetry.io` for a configuration and then posts
usage events from a background thread. A process that imports MLflow before `martian.evals`,
such as an application that serves the core beside its own MLflow code, must set the variable
in its environment.

| Setting | In the evals processes | Effect |
| --- | --- | --- |
| `MLFLOW_DISABLE_TELEMETRY` | `true` unless the environment sets it | MLflow's usage telemetry. `false` turns it back on |
| `DO_NOT_TRACK` | Not set | `true` also turns MLflow's telemetry off |

What each part sends without a setting of yours:

| Part | Sends by itself | Sends only when you configure it |
| --- | --- | --- |
| `mlflow-skinny` | Usage telemetry, off as above | Runs and traces to the MLflow server that `mlflow.tracking_uri` in a suite file or an application's `MLFLOW_TRACKING_URI` names; a Databricks workspace for a `databricks` tracking URI. The local edition's store opens its own SQLite files and ignores the tracking URI |
| `opentelemetry-sdk`, under MLflow tracing | Nothing | Spans to the collector `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT` names |
| `databricks-sdk` and `google-auth`, which MLflow brings | Nothing | Databricks and Google Cloud URIs, which the local and on-premises stores do not use |
| FastAPI, Starlette, Uvicorn, Pydantic, SQLAlchemy, Alembic, HTTPX, Requests, Typer, Rich | Nothing; none has telemetry | — |
| The Evals UI | Nothing. Its scripts, styles and Inter font files are in the wheel, `martian ui` serves them, and the UI calls only `/v1` on its own origin. It has no analytics | — |
| Judges and targets | Nothing | The model provider a judge names and the address of a URL target ([above](#local-suite-runs)) |

`tests/evals/test_no_outbound.py` proves it. It runs a local run of a function target with a
deterministic check, `martian ui` serving the UI and that run, and the core with a process
worker running a function target, each in a clean environment (no CI or pytest variables, under
which MLflow turns its telemetry off by itself, and no telemetry settings) with every connection
and name lookup for an address other than loopback refused. It fails on any such attempt and on
MLflow's telemetry client starting in any of those processes, and checks that the shipped UI
loads nothing from another host.

## The local edition has no login

`martian ui` serves the evals core and the Evals UI for one fixed user with no sign-in. Anything
that reaches its port can read and change all of its data, and send it traces over OpenTelemetry
(`POST /v1/traces`) without credentials. On a laptop it listens on 127.0.0.1
and answers only the `Host` names `127.0.0.1` and `localhost`, so another machine cannot reach it
and a web page cannot reach it through a name that resolves to the laptop. The container image
listens on the container's interfaces, keeps the same `Host` check and prints a warning when it
starts. Publish its port on 127.0.0.1 only and reach it from elsewhere through an SSH tunnel.
Never expose it publicly. See [the local edition in a container](evals-core.md#the-local-edition-in-a-container).

The targets a user defines in its UI keep their keys in `target-keys.json` in the data folder, a
file with mode `0600` that only the user can read. Only the process that runs a run reads a key,
to send it to that run's target in the target's key header; the server's own process never does.
No answer of the server, no log line and no other file in the folder holds a key, traces
included; a run records the key only as the reference `${MARTIAN_TARGET_KEY}`. Anyone who can reach the port cannot read a key back through it, and
cannot point an existing target, and so its key, at another address: a target cannot be edited.
They can still remove targets and define new ones. See [local targets](evals-core.md#local-targets).

## What store errors contain

An error of the evals store or the record store never quotes what was written. When either
refuses a request, its `StoreError` names the operation, the store method such as
`record_trace`, and what was refused. For a write the database refused, that is the table, the
column or constraint when Postgres names one, and the driver's error class and code:
`record_trace: the database refused a value for trace_request_metadata (StringDataRightTruncation, SQLSTATE 22001)`.
It holds no value written: no request, response, case, score, review, id or name. The same text is
the `detail` of the API's `404`, `409` or `422`, the `error` of a failed run, and the worker's log
line.

- The database's and MLflow's own messages are not passed on, because both can quote values:
  SQLAlchemy writes a statement's values into its errors, and Postgres repeats a duplicate key's.
  The one MLflow message passed on is its length refusal, such as
  `'value' exceeds the maximum length of 20000 characters`, which names the field and the limit.
- A `StoreError` does not keep the error it was raised from, not even as a suppressed context,
  so no traceback or error reporter reaches the values. The names above are what an operator
  debugs with.
- Both stores' database engines hide statement values (SQLAlchemy's `hide_parameters`), so a
  database failure that is not a refusal holds none either. The record store raises such a
  failure, a lost connection for example, as SQLAlchemy's own error, and MLflow logs some itself,
  such as SQLite's "database is locked". What they carry is the statement, with placeholders, and
  the database's own message for the failure.
- Not covered: MLflow shortens a trace tag value over 8,000 characters given to
  `EvalStore.set_trace_tag` and logs a warning quoting its first 100 characters. No route sets a
  trace tag. Errors that are not the stores', such as a function target's own exception, which a
  case result keeps as its error, are the target's text.

`tests/evals/test_store_errors.py` writes a canary in writes that are refused: a trace the
database refuses, directly for a metadata value over 8,000 characters and through a run of the
worker for spans holding the prompt, which a trigger refuses, a tag key and a description MLflow
refuses, a case result, a run two writers create at once, a draft case through its route, and a
score whose write fails. It finds the canary in no error, API answer or log line, MLflow's
included, on SQLite and on Postgres.

## Runtime reporting data boundary

Optional `RuntimeReporter` uses a different destination and credential from the probe APIs above: outbound HTTPS to the backend's `/v1/discovery/telemetry-bindings/{id}/resource-events` (v3), authenticated with a file-mounted, binding-scoped `mt_` token. It sends generated event/trace/optional parent UUIDs, model/tool kind, configured provider or connection alias, configured model/tool name, completion time, duration and outcome. It does not export the assessment TraceRecorder, prompts, arguments, results, customer/session identifiers, raw URLs or provider credentials.

Strict allowlisted fields and identifier syntax limit the payload but cannot detect sensitive data deliberately placed in an identifier. The application owns safe naming and separates production and assessment bindings. The backend derives tenant/deployment/environment/version from the credential; these are declared labels, not hardware or binary attestation. Reports remain application assertions. Local buffers are volatile, bounded and single-attempt; failures and drops are counted locally, not reported as a complete backend capture. Backend time-window queries are not a retention policy; automatic expiry is not implemented. See [lifecycle and delivery limits](customer-integration.md#optional-runtime-reporting).

## API keys

An owner/admin issues one organisation key with explicitly selected evals/CI
and/or `redteam:run` capabilities and applicable resource restrictions. The
plaintext is shown once; save it in a secret manager. Lists and later reads
contain metadata only. The key cannot manage the console or mint other keys.
Existing evals tokens retain their original capabilities; use a newly authorized
key to add redteam. Server-side revocation/lifecycle checks apply on each call.
Model entitlement and per-key operational limits remain server-owned.

Legacy direct `martian_redteam.Client` / `martian-redteam` is an explicit operator
compatibility path only. It requires a complete explicit `Settings` pair or
`MARTIAN_REDTEAM_API_URL` / `MARTIAN_REDTEAM_API_KEY`, never generic platform
variables or `.env`. Its key is bound to that selected origin and YAML cannot
change it. Never reuse a platform token as an Agents key. The legacy development
HTTP escape hatch does not apply to modern `MartianConfig`.

The single-key contract applies to evals/redteam APIs. Target-provider, MCP,
runtime reporting and cooperative call-gate credentials have separate scopes.
No hosted/customer readiness is established merely by installing the wheel;
qualify the coordinated services and auth/resource isolation before rollout.

## Retention and AUP

Operation payloads/results and native execution data follow the **service** retention and recovery policy. Backend/Agents must qualify database/checkpoint/Redis cleanup together; SDK local timeouts do not prove server payload deletion. Toxicity and illegal-activity categories will produce disallowed content by design. Requirements on the service side:

- Acceptable-use statement for stored adversarial payloads
- Bedrock-refusal path treated as an **error**, not a silent pass
- Retention/deletion story before public tenants

The SDK accepts only typed operation results. It does not filter attack text or use a provider refusal as a passing judgment.

## Supply chain and CI hygiene

- Pin GitHub Actions by SHA in this repo and in `examples/github-actions.yml`
- `uv.lock` is required in CI (`uv sync --locked`)
- `persist-credentials: false` on checkout
- Contents-read permission on the example workflow
- No `continue-on-error` on the assessment step

## MLflow trace-id path traversal

The `evals` extra pins `mlflow-skinny` 3.18.0, the first release with both fixes below.

- **The advisory.** CVE-2026-53995 ([GHSA-wqmh-qccw-pqwf](https://github.com/advisories/GHSA-wqmh-qccw-pqwf),
  high) covers MLflow's file-based tracking store only. A trace id such as `../../x` sent to
  `POST /api/3.0/mlflow/traces` made `FileStore.start_trace` write trace files outside the
  experiment's folder. The advisory names 3.17.2 as the first fixed release, but its fix
  ([mlflow#26842](https://github.com/mlflow/mlflow/pull/26842),
  [mlflow#26979](https://github.com/mlflow/mlflow/pull/26979)) is not in 3.17.2; it ships in 3.18.0.
- **Not exposed: the SDK uses SQL stores.** The in-process store, the pytest kit and the suite
  runner use SQLite or Postgres, never the file store. A suite file's `mlflow.tracking_uri` can
  name a file store, but then MLflow tracing in the customer's own process chooses the trace ids.
- **The SQL-store twin.** Up to 3.17.2 the SQL store also accepted any trace id and wrote it into
  the trace's artifact location, so `../../x` pointed the trace at a folder outside its
  experiment. 3.18.0 refuses such an id
  ([mlflow#27311](https://github.com/mlflow/mlflow/pull/27311)); the advisory does not mention it.
  `MlflowStore` is not exposed on any release: it gives MLflow only ids of MLflow's own form
  (`tr-` and 32 hex digits) and derives one from any other id.
  `tests/evals/test_mlflow_compat.py` checks both the refusal and the derived ids.
- **The hosted MLflow server** (MorningHQ/martian-evals) runs its own pinned release on Postgres,
  not the file store. Only Backend holds credentials for it, and Backend sends only derived
  `tr-` ids; MorningHQ/MartianBackend's evals workbench guide states that side.

## Licence, attribution and vulnerability reports

Every file in this repository is Apache-2.0 (`LICENSE`); no file carries another licence.
Paid Martian features are not in this repository. Report a vulnerability as
[SECURITY.md](../SECURITY.md) describes, never in a public issue.

`NOTICE` records DeepTeam as the source of taxonomy identifiers and labels. Simulation and evaluation prompts in martian-agents are authored by Martian Security and are not copies of DeepTeam template corpora. Do not paste DeepTeam metric class files into this tree.

## Resource runtime metadata

The optional runtime reporter can export explicitly configured response-ID mappings under the v3 contract. A successful MCP structured response contributes at most ten opaque resource references with configured service/type/action, completion time and reported outcome. It does not export native IDs, arguments or response bodies. References are keyed, binding-scoped HMAC values; they are pseudonymous correlation data, not anonymous data or authorization proof. Missing/error/unmapped calls have unknown resource coverage. Credentials and references change with a new deployment binding. See [configuration and boundaries](customer-integration.md#explicit-resource-response-mappings).

The separate explicit `native_kubernetes=True` mode exports v4 to `/native-events`.
It includes native cluster alias, service-account namespace/name and named request
coordinates. Those identifiers are **not opaque** and may reveal infrastructure
metadata; customer approval of names and binding scope is required. It exports no
credential/header/body/response and does not inspect an existing Kubernetes client.
The application declares the actual identity and request, including on errors.
See [opt-in usage](customer-integration.md#explicit-kubernetes-request-reporting).

## Opt-in call control boundary

The separate `mg_` gate credential exposes only configured target identifiers and
generated process/request UUIDs. It cannot authorize assessments, telemetry, console
reads or control administration. A customer can attach an exact target assignment
to a reporter; selected calls then fail closed without a live decision. This changes
application availability intentionally and remains cooperative instrumentation,
without complete-replica attestation or resistance to direct SDK bypass. Telemetry
revocation and gate revocation have different effects. See the [full lifecycle and
limits](customer-integration.md#sdk-call-gate).
