# Architecture

`martian-sdk` contains `martian.evals`, `martian.redteam` and the combined CI
runner. One scoped API token is bound to one approved Backend origin. The SDK
runs in the customer's process; Backend owns public admission/auth, canonical
eval scores/history and exports; Agents owns hosted attack generation and judging. A version 2
red team run file that names `models` generates and judges locally with the customer's models instead.

```mermaid
flowchart LR
    Customer[Customer Python or CI] --> SDK[Martian SDK]
    SDK -->|local callback| Target[Customer agent and provider]
    SDK -->|Bearer token / typed evals and redteam APIs| Backend[Martian Backend]
    Backend -->|internal bounded operations| Agents[Agents simulator and evaluator]
    Backend --> Scores[Canonical evals and CI sessions]
    Backend --> MLflow[Organisation-bound MLflow]
```

The callback is the only component that calls the customer target. Martian
judging receives the attack, reply and tool identifiers. Provider credentials
stay in the callback's environment; the platform key authorizes Martian APIs.
See [security/data](security-and-data.md) before using production data.

## Execution and result boundary

```mermaid
sequenceDiagram
    participant SDK as Customer SDK
    participant API as Backend
    participant Agents
    participant App as Local target
    SDK->>API: POST /v1/redteam/simulate (operation UUID)
    API->>Agents: Internal bounded simulator operation
    Agents-->>API: Structured result and execution receipt
    API-->>SDK: Operation envelope and attack
    SDK->>App: Attack (optionally locally encoded)
    App-->>SDK: Reply
    SDK->>API: POST /v1/redteam/evaluate (new operation UUID)
    API->>Agents: Internal bounded evaluator operation
    Agents-->>API: Integer score, reason and receipt
    API-->>SDK: Operation envelope, graph/policy revisions
    SDK->>API: Optional one persisted CI session with all probes
```

Both operations have server-owned admission, limits, deadlines and receipts.
SDK execution POSTs are sent once, with same-ID GET recovery after uncertain
transport/gateway delivery. Status polling never calls a model. HTTP phases and
polling share one absolute local wait deadline. Results require a matching
successful operation and valid recovery lifetime; unknown/error/expired results
fail closed. `score=0` is a completed vulnerable judgment and is never retried.

The orchestrator consumes a small `RedteamService` interface; modern Python,
YAML and CI inject the Backend client. The explicit legacy `martian_redteam`
operator client implements the same interface with a dedicated Agents origin/key
and native `/runs/wait`. It is never selected through token detection or an
authentication failure. See [protocol](agent-protocol.md).

## Configuration and reports

`MartianConfig` is the immutable origin/token connection. `CiContext` separately
supplies agent/Git attribution for persisted CI. The old `EvalsConfig` constructor
adapts both, preserving offline and gate options. Standalone redteam/dataset
reads require no CI metadata. Neither path implicitly reads `.env`; modern
clients ignore unused legacy environment variables. YAML cannot redirect a
credential, redirects are disabled, and proxy environment use is opt-in.

A private `SecretStr` redaction policy travels with reports, copies and CI
receipts rather than retaining a client or registering a global secret. Reports
use content digests by default and preserve explicit redaction flags. Raw
in-memory prediction data and local MLflow traces remain private customer data.

## Ownership and source layout

| Path | Responsibility |
| --- | --- |
| `src/martian/config.py` | Shared origin-bound connection, CI attribution |
| `src/martian/redteam/client.py` | Typed public Backend operations and safe recovery |
| `src/martian/ci.py` | One pytest execution and one persisted session |
| `src/martian/evals/` | Dataset/MLflow adapters, gates, Backend uploads |
| `src/martian/evals/model/` | The shared eval data model; see [eval model](eval-model.md) |
| `src/martian/evals/server/`, `src/martian/evals/store/` | The evals core and its store interface; see [evals core](evals-core.md) |
| `src/martian/evals/records/` | The evals core's own records on SQLite or Postgres, their migrations and change events, and the worker's queue |
| `src/martian/_privacy.py`, `result.py` | Scoped redaction and safe atomic receipts |
| `src/martian/redteam/` | The red team core: see [the red team core](#the-red-team-core) |
| `src/martian_redteam/` | Probe engine/catalog, explicit legacy transport, optional runtime/MCP; its names stay importable from `martian.redteam` |
| `src/martian_evals_kit/` | Evals import compatibility |

Taxonomy IDs/types and the generator and judge prompts live in the catalog (`martian.redteam.catalog.prompts`); martian-agents imports the prompts, and hosted model selection lives in Agents.
For local inline evals, customers supply their own criteria and model to
[`response_guidelines`](evals.md#local-response-guideline-judge). Its versioned
grading contract belongs to the SDK; MLflow supplies provider routing and tracking.
The local `response-guidelines-v5` judge cites numbered response segments. The
SDK resolves citations to exact original text and records Unicode code-point
offsets; it does not normalize model quotations or infer verdicts from reasons.
The SDK invokes direct chat completions using MLflow provider routing, rejects
tool requests before dispatch, and validates the full judgment before scoring.
Invalid citations remain scorer errors and fail the gate closed. Citation validity
does not prove the model interpreted a customer's criterion correctly.
For explicit yes/no criteria, `response_conditions` keeps caller-defined
`fail_when` polarity outside the model prompt and maps answers deterministically.
For composite policies or rubrics with direct pass/fail rules and optional source/runtime
context, `response_rubric` (`response-rubrics-v1`) evaluates `verdict="pass"|"fail"|"uncertain"`
directly while keeping numbered segment citation validation.
All three scorers use one model call by default. Opt-in fixed review (`verify=True`)
uses two calls and retains both stages; it never retries until a desired outcome.
A stage re-sends its request only when the provider returned no judgment: a rate limit,
for up to `rate_limit_wait_seconds` as the provider's `Retry-After` asks, or a server error
or connection failure, up to `max_retries` times with backoff.
Invalid or uncertain stages fail closed. Review may change a correct draft to an
incorrect answer; it requires model-specific validation before adoption.
The grounding judges (`groundedness`, `context_relevance`, `context_sufficiency`,
`martian.evals.grounding`) reuse that transport and option checking with one call each and a
flat verdict of strings. Their quotes are copied by the model and must be passages of a named
text (the documents, the response, the request or the expected response) up to whitespace;
a quote that is not, a missing quote or `uncertain` is a scorer error, never repaired.
The conversation judges (`task_completion`, `policy_adherence`, `knowledge_retention`,
`martian.evals.conversation_judges`) read a whole conversation, at `scope: conversation`, with the
same transport, one call each and a flat verdict of strings and turn numbers. The SDK checks each
turn number against the conversation and each quote against the message of the turn it names.
The SDK is not a model host and does not bundle another vendor's prompt corpus.
Package version is `src/martian/_version.py`; catalog version
is independent (`CATALOG_VERSION`); graph/policy revisions come from the service.

## Open-source evals core

This public repository is the home of the open-source evals core. The names stay as they are: the
repository `martian-redteam`, the PyPI distribution `martian-sdk`, the import package `martian`
and the command `martian`. Everything here is Apache-2.0 except the third-party code bundled in
`src/martian/ui/`, a build of the Evals app from the private frontend: the app's own packages are
Apache-2.0, and the third-party packages and the Inter font bundled with them keep their licences,
which the build's `third-party-licenses.md` lists. `NOTICE` attributes both. The core is the same
code in every edition, its paid features behind feature keys; Martian Cloud and On-Premises serve
it, and the features made only for them are built in the private Martian backend and console,
outside this repository, which holds no commercially licensed code
([editions](evals-core.md#editions-and-entitlements)).

The core grows into this layout. Paths marked as planned do not exist yet; the issue named
next to each one creates it.

| Path | Holds | Status |
| --- | --- | --- |
| `src/martian/evals/` | SDK: suite runner (conversations, and [simulated users](evals.md#simulated-users) with personas from real conversations and a [fidelity report](evals.md#simulator-fidelity)), suite file to and from the model, judges, checks, gate | Exists |
| `src/martian/evals/model/` | Shared eval data model used by the SDK and the server ([eval model](eval-model.md)) | Exists |
| `src/martian/evals/server/` | Evals core: API routers and services, identity and tenancy injected ([evals core](evals-core.md)) | Exists: the read and write routes and the OpenTelemetry traces endpoint |
| `src/martian/evals/store/` | Store interface with its in-process bindings | The interface, the in-memory reference store and the in-process `mlflow-skinny` binding (SQLite and Postgres) exist; Martian Cloud's binding is its own, outside this repository |
| `src/martian/evals/records/` | The core's own records (runs, results, scores, reviews, suites, drafts, saved views, judge calibrations and their re-checks) and the worker's queue, portable to SQLite and Postgres | Exists |
| `src/martian/evals/local.py` | The local edition behind `martian ui`: the core on SQLite for one user with no login, with the red team routes, the session call and the built UI from one process ([evals core](evals-core.md#the-local-edition-martian-ui)) | Exists; red team runs are read and annotated, not started |
| `src/martian/evals/local_targets.py` | The local edition's targets in place of an Agents registry: an HTTP endpoint or an OpenAI-compatible model, with the user's key in a file only the user can read, and their `/v1/local/targets` routes ([local targets](evals-core.md#local-targets)) | Exists |
| `src/martian/evals/local_runs.py` | The local edition's worker, which runs each run started in the UI in a process of its own against a local target and scores it with its suite, the `/v1/local/runs` launch and run again ([local runs](evals-core.md#local-runs)), its runs and scoring jobs queued durably in the data folder, and the holds that let `martian ui` take again a run whose `martian ui` stopped, or finish one whose `martian evals run` stopped ([runs whose process stopped](evals-core.md#runs-whose-process-stopped)) | Exists |
| `src/martian/evals/run_record.py` | `martian evals run` recording each suite-file run through the store and the record store, so `martian ui` shows it ([command-line runs](evals-core.md#command-line-runs)) | Exists |
| `src/martian/evals/inspect_import.py` | `martian evals import inspect` reading Inspect AI logs and recording each as a run the same way ([imported Inspect logs](evals-core.md#imported-inspect-logs)) | Exists |
| `src/martian/evals/promptfoo_import.py`, `promptfoo_convert.py` | `martian evals import promptfoo` reading promptfoo results and recording each provider as a run the same way, and `martian evals convert promptfoo` writing a promptfoo config as suite files, listing what does not convert ([promptfoo results and configs](evals-core.md#promptfoo-results-and-configs)) | Exists for results of version 3 and the assertions the built-in checks decide |
| `src/martian/evals/mlflow_import.py` | `martian evals import mlflow` checking an MLflow tracking store's schema revision, reading it over a connection that cannot write, and copying its experiments, traces, datasets, assessments and evaluation runs through the store and the record store ([copied MLflow stores](evals-core.md#copied-mlflow-stores)) | Exists for SQLite and Postgres stores; the hosted MLflow server's REST API is not read |
| `src/martian/evals/data_export.py` | `martian evals export` and `martian evals import martian`: an organisation's data written as JSON Lines files with a manifest, resumable step by step, and imported into a fresh install, through the store and the record store ([data export](data-export.md)) | Exists, for the local edition's data folder and for installs that call it with their own stores |
| `Dockerfile` | The local edition as an image for one user on a small server, with no login ([evals core](evals-core.md#the-local-edition-in-a-container)) | Exists; a release builds it from the wheel it publishes and pushes it to GitHub Container Registry ([releasing](releasing.md#the-local-editions-image)) |
| `src/martian/evals/worker/` | The worker interface the write routes hand runs and scoring jobs to, and the work itself ([the worker](evals-core.md#the-worker)) | Exists: one process per run (`ProcessWorker`) and a one-organisation in-process worker; HTTP targets called by the worker itself, with credentials from its own process, and registered agents through a martian-agents executor interface; per-case setup and teardown hooks and checks installed by name, one of which can be required so that it decides its case's status, which an edition adds and the open edition does not; and a durable queue in the record store's database (`QueueWorker`), whose worker serves beside the app or in a process of its own, so that a restart loses no queued or running run. The Martian server's worker process is MartianSecurity/martian-notes#139's backend part |
| `src/martian/ui/` | The built Evals UI, shipped in the wheel; `martian ui` serves it by default. A build of the Evals app with its Red Team pages, whose source is in MartianSecurity/MartianFrontend (`packages/`), pinned in `pyproject.toml` ([releasing](releasing.md#the-evals-ui-in-the-wheel)) | Exists |

## The red team core

The red team core sits beside the evals core and builds on it: a red team run is an eval run, in the same record store, served beside the evals routes. Everything here is Apache-2.0 and runs without a Martian backend; environments are not part of it.

| Path | Holds | Status |
| --- | --- | --- |
| `src/martian/redteam/model/` | The shared red team data model, and the maps from the version 0.2 report, CI session rows and the engine's campaign summary ([red team model](redteam-model.md)) | Exists |
| `src/martian/redteam/verify/` | The verdict rules and their published test vectors ([verdicts](redteam-verdicts.md)); later the verifiers | The rules exist |
| `src/martian/redteam/store/` | Red team runs in the evals core's record store, as eval runs of kind `redteam` | Exists |
| `src/martian/redteam/server/` | The `/v1/redteam` routes and their frozen contract ([red team API](redteam-api.md)) | Exists; `create_app` mounts them and `martian ui` binds the engine's `LocalLauncher`, so the local edition serves them and starts runs. The hosted backend does not mount them yet (MartianSecurity/martian-notes#299) |
| `src/martian/redteam/runfile.py` | The version 2 run file ([run file](redteam-run-file.md)) | Exists; `martian redteam run` runs it |
| `src/martian/redteam/catalog/` | The catalogue's home in the core: the version 0.2 catalogue of `martian_redteam`, and the generator and judge prompts (`prompts.py`) that the local and hosted engines share | Exists |
| `src/martian/redteam/targets/` | The interface every target implements (`Target`, `TargetReply`): one message in a session, the reply text and its tool calls. The version 0.2 callback and a function in a file (`callback.py`), the HTTP and function targets of suite files and an MCP server target, with conversations, structured tool calls and tool-call evidence from traces ([red team targets](redteam-targets.md)) | Exists |
| `src/martian/redteam/proxy/` | The recording MCP proxy between an agent and its own tool servers: record, inject and block, with a call ledger and local sessions ([red team targets](redteam-targets.md#recording-proxy)). Not an environment | Exists |
| `src/martian/redteam/methods/` | The attack methods and their interface: kinds, arms, transform layers, the context an attempt gives a method, and the registry run files name methods from ([attack methods](redteam-methods.md)) | Exists |
| `src/martian/redteam/engine/` | The engine: the plan, attack, verify loop of a version 2 run (`run.py`, `plan.py`), and each attempt as its method sees it (`attempt.py`), with its budget ledger (`budget.py`), gate and reports; the local service that generates and judges attacks with the user's own models (`local.py`) and the choice between it and the hosted client (`service.py`); `LocalLauncher` for the run routes; and the version 0.2 probe loop | Exists |
| `src/martian/redteam/client.py` | The hosted SDK operations client | Exists |

## CI and scoring

Default baseline probes use one simulator stage, one customer target call and
one evaluator stage. Local encodings do not add model calls. Every planned
probe is retained, including informational outcomes and errors. Native model
attempt accounting is a server responsibility; the SDK does not repair/retry
judgments. Concurrency defaults to two probes.

Dataset exact-match scores are Backend-owned. Redteam/inline checks are recorded
as client-executed/client-scored, even if their judgments originated from Agents.
A session upload is declared evidence, not artifact attestation or deployment
authorization. `martian ci` requires complete local coverage and the Backend
acknowledgment. [SDK and CI](sdk-and-ci.md) defines provenance and gate semantics.

## Independent runtime facilities

The existing optional MCP binding, deployment-bound `mt_` runtime metadata
reporter and cooperative `mg_` call-entry gate keep their separate credentials,
scopes and data boundaries. They neither grant evals/redteam access nor become
covered by the single-key claim. Their host lifecycle, delivery/cancellation
limits and instrumentation boundaries remain documented in
[customer integration](customer-integration.md).

These facilities are integration extras, not a Martian product line.

The package is published on PyPI. Hosted use requires the SDK APIs to be enabled
on the Backend and Agents services for the organisation; see
[real-service qualification](validation.md).
