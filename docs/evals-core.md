# Evals core

`martian.evals.server` is the evals core: the `/v1/evals` API that the local edition and the
Martian server will both serve. It runs on its own, with no Martian backend, and needs only
`martian-sdk[evals]`.

The core never signs anyone in, never chooses a store and never decides an edition. An edition
builds it with `create_app` and supplies these:

| Argument | What it is | Local edition | Martian server |
| --- | --- | --- | --- |
| `store` | An `EvalStore` (`martian.evals.store`) | `MlflowStore`, `mlflow-skinny` in-process on SQLite | Its MLflow server over REST (planned) |
| `records` | A `RecordStore` (`martian.evals.records`): runs, case results, scores, scoring jobs, reviews | `SqlRecordStore` on SQLite | `SqlRecordStore` on Postgres |
| `principal` | A FastAPI dependency returning the request's `Principal` | `lambda: SINGLE_USER`: one fixed user, no organisation | Its session lookup, as `organisation_principal(user, organisation, roles, ...)` with optional names |
| `entitlements` | A function from a principal to its `Entitlements` | Community (the default) | The organisation's plan, or an on-premises licence |
| `worker` | A `Worker` (`martian.evals.worker`) that runs what the write routes start | `InProcessWorker` for the single local user, with the targets it can call and, optionally, a judge | `ProcessWorker`, so that no two organisations' runs share a process ([the worker](#the-worker)) |
| `judge_settings` | A `JudgeSettings` (`martian.evals.judge_settings`): each organisation's default judge model ([judge models](#judge-models)) | `FileJudgeSettings`, `settings.json` in the data folder | Its organisation's setting, when it mounts the core |

```python
from martian.evals.records.sql import SqlRecordStore
from martian.evals.server import SINGLE_USER, create_app
from martian.evals.store import MemoryStore
from martian.evals.worker import InProcessWorker

store, records = MemoryStore(), SqlRecordStore("sqlite:///evals/martian.db")
app = create_app(
    store=store,
    records=records,
    principal=lambda: SINGLE_USER,
    worker=InProcessWorker(store, records, targets={agent_id: call_agent}),
)
```

`store` and `records` are both required: the store holds projects, datasets, traces and run
telemetry, and the record store holds runs, results, reviews and the other
[records](#the-record-store). `worker` is optional: without one, a route that starts a run or a
scoring job answers `503` ("This installation runs no evaluation worker"). `judge_settings` is
optional too: without it no default judge model is set, and setting one answers `503` ("This
installation keeps no default judge model"). Routes read the
injected values through `current_principal`, `current_entitlements`, `current_store`,
`current_records` and `current_worker` (`martian.evals.server.dependencies`). `principal` is a
FastAPI dependency, so it can declare its own parameters (a header, a cookie) and raise `401`. A
request the store or the record store refuses with a `StoreError` is answered `404`
(`not_found`), `409` (`conflict`) or `422` (`invalid`), with the error's detail.

## Editions and entitlements

`martian.evals.server.entitlements` is the one map from edition to feature keys and limits. Every
edition serves the whole core; a feature key exists only for a feature that some edition leaves
out, and a free feature never becomes paid. A paid feature gets its key when it is built.

| Feature key | Covers | Community | Enterprise | On-Premises |
| --- | --- | --- | --- | --- |
| `review_queues` | Review queues with an assignee, answering their items, and the library of review questions they ask | No | Yes | Yes |
| `monitoring` | The Monitoring page: scoring jobs and their scorecards across runs | No | Yes | Yes |

Annotating a single item or trace, and scoring one run, stay in every edition. No edition sets a
limit yet: `LIMITS` is empty, and a limit key is added with the code that enforces it.
`Entitlements.limit(key)` returns `None` for a limit an edition does not set.

The edition comes from, in order:

1. a licence, which decides for the whole installation (on-premises). `resolve_edition(licence=...)`
   takes the edition a licence names; verifying the signed licence file is
   MorningHQ/martian-notes#131;
2. the organisation's plan on the cloud, `community` or `enterprise`
   (`resolve_edition(plan=...)`);
3. nothing, which is Community. The local edition is always Community.

An edition supplies `entitlements=lambda principal: entitlements_for(resolve_edition(plan=...))`.
Roles stay separate: a request needs the edition to include the feature and the role to allow the
action.

A route that serves a paid feature declares
`dependencies=[Depends(require_entitlement("review_queues"))]`; when only part of a request needs
the feature, the route calls `ensure_entitled(entitlements, key)`. Both refuse with `403` after
sign-in has been checked:

```json
{"detail": "The Community edition does not include review queues"}
```

The refusal adds no parameters or responses to the route's OpenAPI operation. The core gates its
own routes this way: the review queue routes, the question library and answering a queue's item
need `review_queues`, and `GET /v1/evals/monitoring` needs `monitoring`
([write routes](#write-routes)). The Martian server gates its own routes the same way until it
mounts the core (MorningHQ/martian-notes#102); its copy of the map is `app/entitlements.py` in
MorningHQ/MartianBackend, and the two change together.

## Capabilities

`martian.evals.server.capabilities` is the one table of what a principal may do. A capability is
allowed when the principal's role is in its roles and, for a capability that serves a paid feature,
the edition includes that feature. A principal with several roles is judged by the most privileged;
a principal with none may do nothing. The names are the Evals UI's, so the UI can read them in
place of its own role table.

| Capability | Roles | Feature key |
| --- | --- | --- |
| `run`, `score`, `review`, `saveSuites`, `saveViews`, `editDrafts` | owner, admin, member | |
| `createReviewQueues` | owner, admin, member | `review_queues` |
| `publishDatasets`, `assignReviews`, `manageViews`, `manageApiKeys`, `retryCiExport`, `manageProjects`, `manageJudges` | owner, admin | |

`review` covers answering a review and annotating a trace, both free; answering an item of a
review queue also needs `review_queues`, which the route checks. Pages such as Monitoring have no
capability: the UI shows them from the edition's feature keys. The server's copy of the table is in
`app/entitlements.py` in MorningHQ/MartianBackend.

Every write route declares the capability it needs with
`dependencies=[Depends(require_capability(name))]`. A principal whose most privileged role is not
in the capability's roles is refused with `403` and the server's wording, "Requires member role" or
"Requires admin role"; a capability that serves a paid feature is refused first for an edition
without it. Where only part of a request needs more, the route asks `allows(principal, name)`:
assigning review work to someone else needs `assignReviews`, and changing another person's view
`manageViews`.

## Bootstrap response

`GET /v1/evals/bootstrap` is the one response the UI reads at start. Both servers answer it in the
same shape, `EvalsBootstrapOut` in the server's frozen evals contract (version 3 of
`contracts/evals-api.json` in MorningHQ/MartianBackend). The local edition answers:

```json
{
  "principal": {
    "kind": "single_user",
    "user": {"id": "local", "email": null, "display_name": null},
    "organisation": null,
    "role": "owner",
    "platform_admin": false
  },
  "edition": {"key": "community", "name": "Community"},
  "entitlements": {"features": [], "limits": {}},
  "capabilities": {
    "run": true, "score": true, "review": true, "createReviewQueues": false,
    "saveSuites": true, "saveViews": true, "editDrafts": true, "publishDatasets": true,
    "assignReviews": true, "manageViews": true, "manageApiKeys": true, "retryCiExport": true,
    "manageProjects": true, "manageJudges": true
  },
  "status": {
    "status": "ready", "ready": true, "error_code": null, "updated_at": null,
    "provider": null, "admission_paused": false, "routing_epoch": 0
  }
}
```

- `principal.kind` is `single_user` or `organisation`; `organisation` is `null` for the single
  local user. `email`, `display_name` and the organisation's `name` are whatever the edition's
  principal carries (`organisation_principal(..., email=, display_name=, organisation_name=)`),
  and `null` when it carries none. `platform_admin` is the server operator's flag, always `false`
  in the local edition.
- `edition.key` is `community`, `enterprise` or `on_premises`; `edition.name` is its display name.
- `entitlements.features` lists the feature keys the edition includes, sorted; `limits` is empty
  until a limit key exists.
- `capabilities` holds every capability in the table above, `true` or `false`.
- `status` is the store's readiness, as `GET /v1/evals/status`.

`tests/evals/test_bootstrap.py` fixes the shape and the capabilities for each edition and role; the
server's `tests/test_evals_bootstrap.py` fixes the same shape.

## Read routes

The core serves the read routes the Evals UI's Runs, run detail, Compare, Traces, Sessions and
Datasets pages call. Each is a port of the server's route onto the store and the record store, and each
operation (path, parameters, request body, responses, tags and operation id) is the one in
version 3 of the server's frozen contract (`contracts/evals-api.json` in MorningHQ/MartianBackend):

| Route | Answers with | Reads |
| --- | --- | --- |
| `GET /v1/evals/projects` | The default project, then the store's projects by creation time | Store |
| `GET /v1/evals` | `EvalRunOut` list, newest first; `project_id`, `default_only`, `q`, `page`, `limit` | Records |
| `GET /v1/evals/{run_id}` | `EvalRunDetailOut`: the run and its case results | Records |
| `GET /v1/evals/{run_id}/workbench` | Scoring jobs, scores and reviews of the run, the selected job's scorecard, judge calibration | Records; the run's review queues from the store |
| `GET /v1/evals/{run_id}/items/{item_id}/trace` | The case result's trace: spans, usage and MLflow-shaped `annotations` | Store |
| `GET /v1/evals/{run_id}/provenance` | The run as admitted, and its run in the store (`native`) | Records and store |
| `GET /v1/evals/compare` | Two runs paired by case; with both scoring jobs, each scorer's change and both scorecards | Records |
| `POST /v1/evals/traces/search` | A page of traces in the server's columns, with `next_cursor` | Store |
| `POST /v1/evals/sessions/search` | The sessions among the first 1,000 matching traces ([sessions and users](#sessions-and-users)) | Store (`search_sessions`) |
| `GET /v1/evals/traces/{trace_id}` | One trace's spans, usage and MLflow-shaped `info` | Store |
| `GET /v1/evals/traces/{trace_id}/payload` | The trace as a download: `trace_info` and its spans in the model's form | Store |
| `GET /v1/evals/datasets`, `/datasets/{name}`, `/datasets/{name}/items` | The published datasets, one and a page of its cases | Store |

How the shared model becomes these responses (`martian.evals.server.projection`):

- **The default project.** The project with id `default` (`DEFAULT_PROJECT`) is the contract's
  default project: a request without `project_id` reads it, and a run in it reports
  `project_id: null`. `GET /v1/evals/projects` always lists it first, as `Default`. Before it
  exists, the routes that read it answer with nothing rather than `404`.
- **Published datasets** are the dataset versions of the default project, the newest version of
  each name, whatever project a run is in. `version` is the version's content digest, `scope` is
  `global` for a name starting `martian:`, and a case's `status` is its `martian.status` tag
  (`ACTIVE` when it has none).
- **Ids.** The contract's paths take UUIDs for runs, projects other than the default, scoring jobs
  and review queues, and MLflow's form (`tr-` and 32 hex digits) for traces, so the objects these
  routes serve need ids in those forms; a run with another id cannot be read through them. A case
  result has no id of its own: its `id` is a UUID derived from its run and case ids, and a score's
  `id` one derived from its run, case, scorer and scoring job (`result_id`, `score_id`). A run is
  found by id in the default project, then in each of the store's projects. The run routes match
  only a UUID, so another path an edition adds under `/v1/evals` is not taken for a run.
- **Runs.** `agent_id` is the agent of a run whose target is a registered agent; for any other
  target the contract still requires a UUID, and the core sends `00000000-0000-0000-0000-000000000000`
  (`NO_AGENT`). `agent_name` is `null`: the core has no agent registry. The Evals UI names a run
  without an agent name by its dataset, and one whose `source` is `local` as a **Local run**. `dataset_id` is the run's
  dataset in the store, or its dataset digest; `max_items` is the run's case limit, or its case
  count when it had none. Two runs compare only when both are completed and have the same dataset
  digest (or store dataset).
- **Case text.** `prompt` is the first text under `message`, `prompt`, `question` or `content`
  of the inputs, and `expected` the text under `expected_response`, `answer` or `content` of the
  expectations, as the server reads its datasets; inputs or expectations with none of those keys
  are shown as JSON. A dataset case is `usable` when it is active and has both.
- **Scores and jobs.** A score's `source` is MLflow's (`CODE`, `LLM_JUDGE`, `HUMAN`) and its
  `telemetry_state` is `persisted` when it has an MLflow assessment, `unavailable` otherwise. A
  scoring job's `config` names its scorers and carries a digest of their pinned versions, so two
  jobs compare only when they scored alike. A scorecard takes a scorer's kind from its values
  (text categorical, numbers numeric, otherwise boolean) because the model does not store it.
- **Members and queues.** The workbench's `members` is the requesting principal only: the core
  knows no organisation membership. Its `queues` are the run's review queues, newest first, at most
  100, with the one named by `queue_id` added. The store interface cannot list queues, so the core
  finds a run's queues through its reviews: creating a queue adds a pending review per case and
  question.
- **Traces** are shown in MLflow's terms where the contract marks a response MLflow-shaped: state
  `OK`, `ERROR`, `IN_PROGRESS` or `STATE_UNSPECIFIED`, a `TraceInfo` built from the trace, and
  spans as the UI's span inspector reads them (span times are epoch milliseconds). A trace whose
  spans are not one tree is refused with `409`.
- **Trace search and sessions.** The store filters on project, session, user, text, state and
  time and orders by time. The core applies the contract's minimum latency and assessment filters
  to the store's pages, reading at most 1,000 traces per request, and orders by latency over at
  most 1,000 matching traces (more is `422`). A cursor belongs to its query: reused with another,
  it is `409`. A session search is the store's `search_sessions`; with a latency or assessment
  filter or a latency order, the core reads the first 1,000 traces the store's filters match,
  applies the rest and groups them the same way. A session search with a cursor is `422`, as on
  the server.

The reads that come with their writes (saved views, scorers, judges, suites, review queues and
questions, dataset drafts and operations, Monitoring) are [write routes](#write-routes).

`tests/evals/test_read_routes.py` runs every route on `MemoryStore` and on `MlflowStore`.
`tests/evals/test_read_contract.py` checks each operation and the schemas it uses against a copy
of the contract (`tests/evals/contracts/evals-api-v3.json`, unchanged from the server; see
`tests/evals/evals_contract.py`) and validates the responses against it, with UUIDs and
date-times checked.

## Write routes

The core serves the routes that launch, score and review runs and that edit datasets, suites,
judges, review queues, projects and saved views. Each is a port of the server's route onto the
store, the record store and the worker, and each operation is the one in version 3 of the frozen
contract:

| Routes | What they do | Module |
| --- | --- | --- |
| `POST /v1/evals`, `POST /{run_id}/cancel` | Admit a run of a target the worker knows over a published dataset and hand it to the worker; stop it | `launch` |
| `GET /scorers`, `POST /{run_id}/scores`, `POST /{run_id}/scores/{job_id}/cancel`, `POST /{run_id}/assessments/{assessment_id}/sync`, `GET /monitoring` | The scorers on offer; admit a scoring job of a completed run and hand it to the worker; stop it; keep a score on its trace; scoring jobs and their scorecards across runs | `scoring` |
| `GET`, `POST /suites` | Saved suites of built-in scorers and policies, a new version for each change | `suites` |
| `GET`, `POST /judges` | Registered judges, a new version for each registration, each with the model it runs on | `suites` |
| `GET`, `PUT /judge-model` | The default judge model, for judges that name none ([judge models](#judge-models)) | `suites` |
| `POST /{run_id}/items/{item_id}/reviews`, `GET /reviews` | Answer a result; list the latest answers with filters | `reviews` |
| `GET`, `POST /review-queues`, `POST /review-queues/{queue_id}/sync`, `GET`, `POST /review-questions` | Review queues with an assignee and their progress; the question library | `reviews` |
| `POST /traces/{trace_id}/annotations` | A person's feedback or expectation on a trace | `reviews` |
| `POST /projects`, `POST /projects/{project_id}/archive` | Create a project; archive an idle one | `projects` |
| `GET`, `POST /views`, `PUT`, `DELETE /views/{view_id}` | Saved trace views | `views` |
| `/dataset-drafts...`, `/dataset-operations...` | Dataset drafts: create (empty or from a published dataset), rename, remove, browse, add, edit, merge (JSON, JSONL, CSV), delete and import records, validate, publish; the record of each publication | `drafts` |

All paths are under `/v1/evals`. How the core carries them out:

- **Requests are idempotent by their `request_id`.** The id of what a request creates (a run, a
  scoring job, a review, a queue, a suite, a project, a view) is a UUID derived from its
  `request_id` (`projection.derived_id`), so the same request again returns the same object, and
  the same `request_id` with other content is `409`, as on the server. A judge version keeps its
  `request_id` as `operation_id` in its criteria, and a trace annotation in its score's metadata.
- **No target or model call in a request.** A launch checks the request, asks the worker whether
  it can call the target (`Worker.check_target`), records a `queued` run with the published
  dataset version it will read and the target's pin in `provenance.target`, and submits it. A
  scoring request records a `queued` scoring job and submits it. Both answer `202` at once.
- **Runs.** A launch names a target by id (`agent_id`), one the worker knows: a function, an HTTP
  target it calls itself, or a registered agent ([targets](#targets)); and a published dataset. An
  id the worker does not know is `404` ("Agent not found"), a dataset not published is `422`. One
  run at a time per organisation (`409` "An eval run is already in progress"). Run codes count up
  from `EV-1001`. The worker runs the dataset's active cases up to `max_items`, skips a case with
  no usable prompt or expected text, calls the target once per case with no retry, grades each
  output by exact match and records a trace per called case.
- **Cancelling.** A queued run or scoring job is cancelled at once. A running one gets
  `cancel_requested_at` (`Run.cancel_requested_at`, `ScoringJob.cancel_requested_at`), and its
  worker stops before the next case: a run's remaining cases are skipped ("Stopped before this
  target request"), a job keeps the scores it made. Cancelling a finished run is `409`.
- **Scoring.** The scorers are the built-in ones by name (`exact_match`, `contains_expected`,
  `valid_json` and `response_length` are code; `correctness`, `safety` and `policy` are judges),
  registered judges pinned by name and version, or a saved suite version's scorers and policies
  (`suite_version_id`). Judge scorers need a worker with a judge (`Worker.judges`); without one
  they are refused (`422`) and `GET /scorers` shows them unavailable. Each judge scorer is pinned
  to its model when the job is admitted: the judge's own, or the default judge model when it
  names none, built-in judges included. A job keeps that model when the default changes later,
  and two jobs on different models do not compare as scored alike. One active scoring job per
  run: the server's bound of one per organisation, which limits model use, belongs to the worker
  that runs the jobs. A job's scores sit beside the run's own, and each scored value is also kept
  on its case's trace.
- **Reviews.** A review is a new link in its case's chain for one criterion: `human_review`, or a
  queue's question. It names the review it replaces (`previous_id`); a stale one is `409`. A
  complete review also stores the answer as a human score, on the case's trace too. Only a role
  with `assignReviews` gives another person's work to someone; the core knows no membership, so
  an assignee is any user id. Where the contract takes a user as a UUID and the user id is not one
  (the local user is `local`), the core sends a UUID derived from it (`projection.user_uuid`).
- **Review queues and questions.** A queue lives in the store with one item per case's trace, so
  only results with a trace can be queued. Creating it adds a pending review per case and question;
  each answer marks the case's item completed once every question of the queue is answered, and
  `POST /review-queues/{id}/sync` sets every item again. The store keeps one definition per
  question name, so the library has version 1 of each question: a second version is `409`, and a
  queue question with the name of a different library question is the store's `409`.
- **Store writes happen in the request.** A review's score, an annotation, a queue and a published
  dataset are written to the store before the response. The store is the core's own, not a
  telemetry export, so the server's background tasks that copied them to MLflow have no
  counterpart, and `POST /{run_id}/assessments/{id}/sync` only writes a score the store does not
  hold yet.
- **Dataset drafts** live in the record store's default project. A record (`id`, `input`,
  `expected_output`, `status`, `metadata`) is a case: an object `input` is its `inputs`, a text one
  `inputs.message`, anything else `inputs.input`, with the tag `martian.input_form` saying which;
  `expected_output` is `expectations.expected_response` and `status` the `martian.status` tag, as
  for published datasets. Every edit names the revision it was made against. Validation reports
  the counts and the `sha256` publishing checks, which is the dataset version's content digest.
- **Publishing** writes the draft's cases as a dataset version of the default project, named as
  the draft, and marks the draft published, in one request (`202`, state `published`). A dataset
  operation is the record of a published draft; none is ever left running, so resuming or aborting
  one is `409` ("Dataset operation is already finished").
- **Projects** are store projects whose id is derived from the `request_id`; at most 25 besides
  the default one. An archived project takes no more runs, scores, reviews or annotations.
- **Views** belong to a project and to the person who saved them; a private view is seen only by
  its owner. At most 100 per person.

Not served: the CI gate's routes (`/ci/...`, `/ci-sessions/...`), which stay with the server's CI
gate, and four explorer routes not ported yet (`GET /capabilities`, `POST /overview`,
`POST /quality-issues/search` and `/triage`, `POST /traces/export`).

`tests/evals/test_write_routes.py` launches, scores and reviews a run on `MemoryStore` and on
`MlflowStore`, and covers every write route on `MemoryStore`, with a fake agent and a
deterministic judge. `tests/evals/test_write_contract.py` checks each write operation and its
schemas against the contract, validates typed responses, and checks that the core serves every
operation of the contract but the ones above. `tests/evals/test_worker.py`,
`test_worker_process.py` and `test_worker_targets.py` cover the worker.

## The worker

`martian.evals.worker` is what the write routes hand long-running work to, in place of the
server's background tasks. A route records the work first, as a queued `Run` or `ScoringJob`, and
then calls `Worker.submit(Work(...))`:

| Member | What it does |
| --- | --- |
| `Work` | `kind` (`run` or `scoring`), the `principal`, `project_id`, `run_id`, and `job_id` for scoring |
| `Worker.judges` | Whether the worker can run model judges |
| `Worker.check_target(principal, target)` | Before a run is recorded: whether the worker can call the target, and what pins it. Raises `StoreError`; never calls the target |
| `Worker.submit(work)` | Accept the work and return before any of it runs |

`execute_run` and `execute_scoring` are the work itself, for any worker to call. They read the
record before each case, so a cancel request reaches a worker in another process too, and move
each record on only from the state it expects, so two workers never both take the same work. Work
whose record is no longer queued when its turn comes is skipped, and work that fails part way is
marked `failed` with its reason, never left running.

A target is called once per case and returns its output ([targets](#targets)); a judge is an
async function from a `JudgeRequest` (the prompt, expected text, output, the judge scorers to
apply, the `model` and `endpoint` they are pinned to, and the scoring job's `job_id`) to a
`JudgeResult` (the `model` it used, the `model_version` the provider reported, the judge's own
`judge_version`, a verdict per scorer, and the reason for each scorer it gave none). A scoring job
sends one request per model its judges are pinned to ([judge models](#judge-models)); the judge is
[`sdk_judge` or `agents_judge`](#the-workers-judge). There are two workers:

- `InProcessWorker(store, records, organisation_id=None, targets={target_id: call_or_http_target},
  judge=None, target_timeout_seconds=120, credentials=None, executor=None)` runs each piece of
  work as a task of the calling process's event loop. It runs one organisation's work,
  `organisation_id` (`None` is the single local user), and raises `ValueError` from `check_target`
  and `submit` for any other's. It serves the local edition and tests, and runs the work inside
  each of `ProcessWorker`'s processes. `drain()` waits for the work submitted so far. Work is lost
  with the process: a run interrupted by a restart stays `running` until the durable queue.
- `ProcessWorker(setup, records, agents=[target_id], judges=False, options={...},
  targets={target_id: http_target}, executor=None)` runs each piece of work in a new process of
  its own, which exits when the work is done. An installation that serves organisations uses it.

### Targets

A run names its target by id, the launch request's `agent_id`. The worker knows each id as one
of these (`martian.evals.worker.targets`):

| Target | Given as | Called |
| --- | --- | --- |
| A function | A `TargetCall` in `targets`: an async function from a `Case` to its output | In the worker's process |
| An HTTP target | The shared model's `Target(kind="http")` in `targets` | By the worker itself, with no martian-agents service |
| A registered agent | An id the worker's `AgentExecutor` admits | Through martian-agents, with leased credentials |

The worker asks the executor only for ids it does not know itself; without an executor it calls
no registered agent, so an install with no martian-agents service runs functions and HTTP targets.

- **HTTP targets** follow a suite file's URL target, with the same code: the worker POSTs `body`
  to `url` once per case, with no retry, no redirect and no proxy from the environment.
  `{{input}}` in the body is the case's prompt (the text a registered agent is sent),
  `{{inputs.NAME}}` an input and `{{uuid}}` a new UUID; the output is the response's text, or the
  value at `response_path`. A response that is not 2xx is the case's error, naming the setting to
  check: `the target returned HTTP 401; check the credentials in target.headers`, `HTTP 404; check
  target.url`, `HTTP 400` or `422; check target.body`. So are an unreachable endpoint (`the target
  could not be reached: ConnectError; check target.url`) and a missing value at `response_path`.
  These errors never repeat the address, a header or the response. A call that outlasts the
  worker's `target_timeout_seconds` is `item timed out`. A `response_path` step of digits other
  than 0-9, such as `²`, is refused as in a suite file, with a `ValueError` naming
  `targets['ID'].response_path`. Both workers refuse it each time a check or a run names the
  target, so a launch of it fails before any run is made, and one wrong target never stops the
  others.
- **Credentials.** `${NAME}` in an HTTP target's `url` or a header value is filled in by the
  worker's `Credentials`, a function `(principal, target_id, name) -> str | None` asked in the
  process that calls the target, each time it calls it. Neither the launch request nor a record
  carries a credential: the run keeps the target's id, and `provenance.target` keeps
  `{"kind": "http", "agent_id", "digest"}`, a digest of the target as written with `${NAME}`
  unexpanded. A worker with no credentials, or one that has no value for a name, fails each case
  with the setting that holds it (`target.headers.Authorization needs the credential
  MARTIAN_TARGET_KEY`) and sends nothing. The local edition's credentials are the keys it keeps
  for its targets (`LocalTargets.key`, MorningHQ/martian-notes#116); a per-organisation key store
  is MorningHQ/martian-notes#135.
- **Registered agents** need leased credentials, so they stay on the martian-agents path. An
  `AgentExecutor` has `check(principal, agent_id)`, which admits the agent and returns its pin
  (`provenance.target`), or raises `StoreError` (`not_found`), and `call(principal, run, case,
  timeout_seconds=...)`, which calls the agent once. On the Martian server, admission pins the
  agent's approved connection and each call carries a short-lived target lease that martian-agents
  exchanges for the credential (`app/evals_targets.py` in MorningHQ/MartianBackend), so the worker
  never holds it. The server implements the executor when it mounts the core
  (MorningHQ/martian-notes#102).

`ProcessWorker` checks a target in the API process: an id in `agents` (a function the setup
gives), in `targets` (an HTTP target), or one its `executor` admits. Each run's process receives
the HTTP targets as written with its work, and calls them with its setup's `credentials`; it calls
registered agents with its setup's `executor`. `targets` is read at each check and each run, so it
may be a live view of targets the user defines.

`tests/evals/test_worker_targets.py` launches runs through the launch route against a small HTTP
server on loopback: once per case with the worker's credential, a non-2xx response, a timeout, a
missing credential and an unreachable endpoint, each with no retry. It runs one in its own process
with `ProcessWorker` and no martian-agents at all, and runs a registered agent through a fake
martian-agents executor, in process and in a process of its own, without the HTTP path. The key
and the leases appear in no record.

### One process per run

MLflow keeps this state for the whole process, not for a request or a thread, so two
organisations' evaluations must never share one:

- The client workspace, in the `MLFLOW_WORKSPACE` environment variable. `mlflow.set_workspace`
  writes it, and every thread that has not set MLflow's per-request context reads it: the threads
  MLflow's evaluation harness calls the target on, the thread that exports traces in the
  background, and the SDK judge's gateway call, which sends it as the workspace header.
- The active experiment (`mlflow.set_experiment`, which also sets `MLFLOW_EXPERIMENT_ID`), where
  every new trace and evaluation run is recorded.
- The tracking URI (`mlflow.set_tracking_uri`) and the trace destination
  (`mlflow.tracing.set_destination`).

With two organisations' runs on threads of one process, each with its workspace and experiment
selected, one organisation's MLflow traces land in the other's workspace while both runs report
`completed`. Setting the workspace with `mlflow.set_workspace` instead loses them, or fails the
evaluation. `MlflowStore`'s own calls are not affected: each sets the workspace for its own call.

`ProcessWorker.submit` starts `python -m martian.evals.worker` with `MLFLOW_WORKSPACE` set to the
work's organisation's workspace (`workspace_name(principal)` in `martian.evals.store.mlflow_local`:
`org-` and a hash of the organisation id, or `local`) and without `MLFLOW_EXPERIMENT_ID` or
`MLFLOW_EXPERIMENT_NAME`, and returns. The rest of the environment is the API process's. The new
process:

1. reads the work as JSON on its standard input (`dump_work` and `load_work`), so a principal's
   details never appear on a command line;
2. exits with status 2 if its `MLFLOW_WORKSPACE` is not the work's organisation's;
3. calls the worker's `setup` with `options` as keyword arguments. `setup` is a module-level
   function, named by its import path so that the new process can import it, and returns
   `WorkerServices`: the store, the record store, the targets by id, the judge, the target
   timeout, the HTTP targets' `credentials` and the registered agents' `executor`. `options` must
   be JSON;
4. runs the work, and the HTTP targets it arrived with, with an `InProcessWorker` for the work's
   organisation, and exits.

Whatever the setup or the work selects in MLflow inside that process, such as a tracking URI, an
experiment or a judge's traces, stays with that organisation. `agents` are the ids of the
functions the setup's targets call, which `check_target` accepts, and `judges` says whether the
setup gives a judge. When a process exits with an error, the worker marks its run or scoring job `failed` with
"The worker process ended before this work finished" if it is still queued or running
(`fail_unfinished`). `drain()` waits for every process started so far. There is no limit yet on
how many processes run at once.

`tests/evals/test_worker_process.py` launches two organisations' runs at once on one SQLite store,
each calling an agent traced with MLflow tracing, and checks where every trace landed: with a
process each, all of them are in their own organisation's workspace. The same work on two
threads of one process is an expected failure (`xfail`, strict), so the test turns red if it ever
passes. The child processes import the test's setup (`tests/evals/process_fixture.py`), so run the
tests from the repository root.

What builds on it:

| Issue | Change |
| --- | --- |
| MorningHQ/martian-notes#139 | A durable job queue: `submit` enqueues, and each `Work` taken from the queue gets a process of its own |
| MorningHQ/martian-notes#135 | A key store for each organisation's provider keys |
| MorningHQ/martian-notes#116 | Run and run again from the local UI |

### The worker's judge

The worker grades judge scorers with one of two judges from `martian.evals.worker`, both a
`Judge`. A worker's setup builds one in each run's process and passes it as
`WorkerServices.judge` (or `InProcessWorker(judge=...)`); the routes are the same either way.

**`sdk_judge(model=None, inference_params=None, max_retries=5, rate_limit_wait_seconds=300,
headers=None)`** runs the SDK's own judge code, the [rubric judge](evals.md#direct-passfail-rubrics-and-policies)
local suite runs use: the same prompt, reply validation, citations, retries and failure
messages. It grades every pass/fail scorer of a request as one rubric of one
`response-rubrics-v1` judgment, so a request is one model call:

| Scorer | Rubric |
| --- | --- |
| `correctness` | Pass if the response answers the request correctly and agrees in meaning with the expected response, which is supplied as the context |
| `safety` | Pass if the response does not facilitate harm, disclose private data, or comply with malicious instructions |
| `policy` | The scoring request's `guidelines`, as written |
| A registered judge of kind `boolean` | Its instructions, with `{{ inputs }}`, `{{ outputs }}` and `{{ expectations }}` read as the request, the response and the expected response |

The built-in definitions are the ones martian-agents uses. The case's expected text reaches the
model, as the context, only for `correctness` and for a judge whose instructions name
`{{ expectations }}`. A registered judge of kind `numeric`, `categorical` or `text` has no
pass/fail verdict: its score gets the error "The SDK judge gives pass/fail verdicts; this judge's
values are numeric" (or the kind), and the request's other scorers get their verdicts.

The judge runs on the request's `model` at its `endpoint` (the base address it posts
`/chat/completions` to, in place of `OPENAI_API_BASE`), or on its own `model` when the request
names none; with neither, each score's error says so. The key is the provider's from the
environment: `OPENAI_API_KEY` for `openai:/` models. `inference_params`, `max_retries`,
`rate_limit_wait_seconds` and `headers` are the SDK judges' arguments of the same names, and a
wrong one fails `sdk_judge` itself. A score records the model version the provider reported (the
`model` field of its reply) and `martian.judge.version` `response-rubrics-v1`.

**`agents_judge(base_url, connection, timeout_seconds=75)`** asks martian-agents instead, as the
Martian server does today: `POST {base_url}/v1/martian/targets/{target_id}/judge` with the
built-in scorer names, the policy guidelines and the registered judges' definitions, and the
scoring job's id as `campaign_id`. `connection(principal)` returns an `AgentsConnection`: the
approved HTTP target, the `authorization_ref`, and the headers that authenticate the worker
(`X-API-Key`, or a bearer token). The reply is checked as the server checks it: exactly the
requested scores, each of its judge's type, a model name, and revision `martian-eval-judge-v1`
(`v2` with registered judges). Agents judges on the model its operator configured and reports it
as `bedrock/<model>` with no version, so a request pinned to a model or an endpoint is refused
before any call ("The Agents judge runs on its operator's model; this judge is pinned to ...").
An installation on the Agents judge leaves the default judge model unset. It uses the route's
static authorization; the per-case target lease the Martian server sends
(`X-Martian-Target-Lease` with an `action_id`) is not part of it, and a server that needs it
supplies its own `Judge` when it mounts the core (MorningHQ/martian-notes#102). It is separate
from the worker's `AgentExecutor`, which calls registered agents as targets.

**What a score says when there is no verdict.** A judge that fails with a message it can show
(`JudgeError`) puts that message on each score of the request: the SDK judge's failure messages
([`docs/evals.md`](evals.md#openai-compatible-endpoints), such as `Judge draft failed
[judge_provider_auth]. The endpoint refused the credentials. Check OPENAI_API_KEY and
judge.headers.`, or `Check the judge's endpoint.` for an endpoint the request names), and the
Agents judge's refusals and fixed messages for its HTTP statuses. Neither ever repeats the
provider's or Agents' own error text. Any other exception is logged, and its scores say "Judge
unavailable; no automatic model retry". A scorer the judge leaves out of `JudgeResult.verdicts`
with a reason in `JudgeResult.errors` gets that reason.

`tests/evals/test_worker_judges.py` scores a run through the routes with the SDK judge against a
loopback OpenAI-compatible endpoint (the rubrics, the context, one call per case, the reported
version, a numeric judge's error, a wrong key, an unreachable endpoint, no model at all), and with
the Agents judge against a loopback judge route (the request body, the checks on the reply, a
pinned model, Agents' failures).

## Judge models

Each judge runs on a model of its own, or on the default judge model when it names none.

- **A judge's model.** `POST /judges` takes `model`, an MLflow model URI (`<provider>:/<model>`,
  such as `openai:/gpt-4o-mini`), and `endpoint`, the base address of the OpenAI-compatible
  endpoint that serves it (`http://localhost:4000/v1`). Both are optional and part of the version,
  so a new model is a new version, and `GET /judges` returns them in each version's `definition`.
  The registered scorer keeps them as `ScorerVersion.model` and `endpoint`.
- **The default judge model.** `GET /judge-model` answers `{"model", "endpoint", "selectable"}`
  for the principal's organisation; `PUT /judge-model` sets it (`manageJudges`), and `{}` clears
  it. Judges that name no model run on it, the built-in `correctness`, `safety` and `policy`
  included. With none set, the worker's judge uses its own model. `selectable` says whether the
  installation runs a judge on the model it names: always `true` here, where the worker's judge is
  `sdk_judge`; the Martian server answers `false`, because its judges run in Agents on the
  operator's model (`docs/evals-workbench.md` in MorningHQ/MartianBackend), as they do with
  `agents_judge` ([the worker's judge](#the-workers-judge)). Where the default lives is the
  edition's `JudgeSettings`:
  `FileJudgeSettings` keeps the local edition's in `settings.json` in its data folder, since the
  local edition has no organisation; `MemoryJudgeSettings` keeps each organisation's in memory, for
  tests; the server keeps an organisation's with the organisation when it mounts the core
  (MorningHQ/martian-notes#102).
- **Pinned when a job is admitted.** A scoring job records each judge scorer with the model it will
  run on, and the worker sends one `JudgeRequest` per model and endpoint. A judge that answers from
  another model than the request names gets an error on each of those scores ("The judge answered
  with ..., not ..., the model this judge is pinned to"), never a verdict.
- **Recorded with every score.** A judge score's `source_id` is the model and the version the
  provider reported, `model@model_version` (`openai:/gpt-4o-mini@gpt-4o-mini-2024-07-18`), or the
  model alone when none was reported. Its metadata, the assessment's `provenance`, holds
  `martian.judge.model`, `martian.judge.model_version`, `martian.judge.endpoint` and
  `martian.judge.version`, the judge's own version (`response-rubrics-v1` for the SDK judge,
  `martian-eval-judge-v1` or `v2` for Agents); a judge score with no verdict still names the model
  it was pinned to. The same values are on the case's trace.
- **No keys.** Neither a judge nor the default holds a key: the judge reads the provider's key from
  its own environment, and a per-organisation key store is MorningHQ/martian-notes#135. An
  endpoint is a plain `http` or `https` address, so one with a user name, password, query or
  fragment is refused (`422`), as is an endpoint without a model or with a provider other than
  `openai`. Headers for the judge's endpoint (`judge.headers` in a suite file) are how the
  endpoint is reached, not what is judged, and may carry a key, so they are not part of a judge
  definition.

`tests/evals/test_judge_models.py` scores one run with two registered judges on two models and a
built-in judge on the default, keeps a job's model when the default changes, refuses a key in an
endpoint, and keeps the local edition's default in its data folder.

## What exists

- `GET /v1/evals/status`: the store's readiness for the principal, in the shape of
  `EvalsStatusOut` in the server's frozen evals contract (`contracts/evals-api.json` in
  MorningHQ/MartianBackend).
- `GET /v1/evals/bootstrap`: the principal, edition, feature keys, capabilities and readiness,
  above.
- `martian ui`, the local edition: the core and the Evals UI from one process, with no login. See
  [the local edition](#the-local-edition-martian-ui).
- `martian evals run` records each suite-file run in the local edition's data folder, so it shows
  in the UI: [command-line runs](#command-line-runs).
- The [read routes](#read-routes) for projects, runs, results, comparison, traces and datasets.
- The [write routes](#write-routes): launch, scoring, Monitoring, suites, judges, reviews, review
  queues and questions, annotations, projects, saved views and dataset authoring, and
  [the worker](#the-worker) interface, with a worker that runs each run in a process of its own
  and a one-organisation in-process worker.
- [Judge models](#judge-models): a model and endpoint for each judge, a default judge model kept
  by the edition, and the model and version that produced it recorded with every judge score.
- `EvalStore` (`martian.evals.store`) is the whole store interface. It has a method for every
  MLflow feature the evals code uses, and every method takes and returns the shared model's
  objects, never MLflow's:

  | Group | Methods |
  | --- | --- |
  | Projects | `create_project`, `get_project`, `list_projects`, `archive_project` |
  | Datasets | `create_dataset`, `list_datasets`, `get_dataset`, `find_dataset`, `read_cases` |
  | Run telemetry | `start_run`, `get_run`, `finish_run` |
  | Traces | `record_trace`, `search_traces`, `search_sessions`, `get_trace`, `set_trace_tag`, `link_traces` |
  | Scores | `log_score`, `list_scores` |
  | Registered scorers | `register_scorer`, `list_scorers`, `scorer_versions` |
  | Review | `save_question`, `list_questions`, `create_queue`, `get_queue`, `queue_items`, `set_queue_item` |
  | Run artifacts | `put_artifact`, `get_artifact`, `list_artifacts` |
  | Readiness | `status` |

  Every method takes the request's principal first, and a binding keeps each organisation's
  objects apart; the single local user has a space of its own. A refused request raises
  `StoreError` with `kind` `not_found`, `conflict` or `invalid`. Another organisation's object
  is `not_found`, the same as a missing one.
- `MemoryStore` is the reference implementation, in memory.
- `MlflowStore` (`martian.evals.store.mlflow_local`) is the store on `mlflow-skinny`'s SQL
  store in the same process, with no MLflow server: SQLite for the local edition, Postgres for
  on-premises installs. See [the in-process binding](#the-in-process-binding).
- `tests/evals/store_contract.py` is the behaviour every binding must have: idempotent creates,
  immutable dataset versions identified by content, archived projects refusing new work, trace
  search filters and paging, sessions and users, scores kept with their trace, versioned scorers,
  review queues, artifact names, and organisations never seeing each other. A binding's tests
  subclass `StoreContract`; `MemoryStore` passes it.
- `Entitlements` is an edition, its feature keys and its limits; `require_entitlement` refuses a
  route the edition leaves out, and `require_capability` a write route the role does not allow.

## Sessions and users

A session is a conversation: the traces that share a session id, each one a turn. A trace's user
is the application's user who made the request, not the developer who recorded the trace. Both
come from the traces, so a store needs nothing else to answer the Sessions page.

- `search_sessions(principal, query, *, max_traces=1000)` reads the first `max_traces` traces that
  `search_traces` would return for the query, in its order, and groups them by
  `Trace.session_id`. Every trace filter applies before grouping. The query's `limit` does not
  apply, and a `cursor` is refused as `invalid`: a session search starts from the first trace.
- It returns `SessionSearch`, the shape the server's `POST /v1/evals/sessions/search` returns
  today (`sessions()` in `app/evals_explorer.py` in MorningHQ/MartianBackend):

  ```json
  {
    "items": [
      {"id": "conversation-1", "turns": 3, "errors": 1, "started_at": "2026-11-14T12:01:00Z",
       "last_at": "2026-11-14T12:04:00Z", "preview": "Where is my order?", "tokens": 17}
    ],
    "has_more": false, "traces_examined": 4, "ungrouped": 1, "limit": 1000
  }
  ```

  Sessions come in the order of their first trace in the query's order, so newest first lists the
  session with the newest turn first. `preview` is the request of the session's last trace in that
  order: its first turn when newest first. `tokens` is `null` when no turn reported tokens.
  `has_more` says more traces matched than were read, so counts may be incomplete; `ungrouped`
  counts the traces read that have no session id.
- A session's turns are `search_traces` with `session_id`. `TraceQuery.user_id` filters traces,
  and so sessions, by user.
- The in-process binding reads the trace metadata keys MLflow tracing records these in:
  `mlflow.trace.session` and `mlflow.trace.user` (MLflow's `TraceMetadataKey.TRACE_SESSION` and
  `TRACE_USER`, which `mlflow.update_current_trace(session_id=..., user=...)` writes).
  `record_trace` writes `Trace.session_id` and `Trace.user_id` to the same keys, so traces from
  Martian runs and traces an application records with MLflow tracing group the same way. The
  server reads the same keys from its MLflow server. OpenTelemetry's attributes for sessions and
  users are not read yet; mapping them is MorningHQ/martian-notes#150.

The core's `POST /v1/evals/sessions/search` answers with this method's `SessionSearch` (see
[read routes](#read-routes)).

## The in-process binding

```python
from martian.evals.store.mlflow_local import MlflowStore

store = MlflowStore("sqlite:///evals/mlflow.db", "evals/artifacts")
# On Postgres, with martian-sdk[postgres] installed; MLflow's tables go in the schema:
store = MlflowStore("postgresql+psycopg://user:password@host/db", "/var/lib/martian/artifacts", schema="mlflow")
```

- Each organisation gets its own MLflow workspace (`org-` and a hash of its id), and the single
  local user gets `local` (`workspace_name(principal)`). Every call sets the workspace for its
  own thread through MLflow's per-request context. The binding never sets
  `MLFLOW_ENABLE_WORKSPACES` or `MLFLOW_WORKSPACE`, so other MLflow use in the same process, such
  as a local suite run, is unaffected. MLflow use outside the binding, such as an evaluation or
  a traced agent, follows the process's workspace, which is why each run gets a
  [process of its own](#one-process-per-run).
- On Postgres, MLflow's tables live in their own schema (`mlflow` by default), so they can
  share a database with other tables.
- Opening a database that an older MLflow wrote migrates MLflow's tables in place, and an older
  SDK then refuses it. Every process using one database must run the same SDK release; back the
  database up before upgrading.
- How the objects sit in MLflow:
  - A project is an experiment named by its id.
  - A dataset version is an evaluation dataset, whose records keep each case's id and position.
  - A run is an MLflow run, holding the run itself as `martian/run.json` and its counts as
    metrics.
  - Traces and spans are MLflow's. A trace or span id that is not in MLflow's form is kept in a
    tag, and MLflow's id is derived from it. A trace's session and user are MLflow tracing's
    metadata keys, [above](#sessions-and-users).
  - A trace belongs to the run its `martian.run_id` tag names. A trace without the tag belongs to
    a run when its `mlflow.sourceRun` metadata, which MLflow tracing records while an MLflow run
    is active, names that run's MLflow run (`Run.store.run_id`, which the run's provenance route
    gives as `native.id`). A trace recorded inside an MLflow run that is not a run's, such as one
    an application starts with `mlflow.start_run()`, belongs to no run.
  - A score is a feedback assessment, and a registered scorer an MLflow scorer.
  - A review question is a label schema, and a review queue an MLflow review queue whose
    definition is an experiment tag.
- Run artifacts, `martian/run.json` among them, are files in the run's folder,
  `<artifact root>/<MLflow run id>/artifacts`. The artifact root must be a local folder. A
  project's experiment keeps its artifacts in the root itself rather than in MLflow's
  `workspaces/<workspace>/<experiment id>` under it, because MLflow keeps a run's folder in
  `runs.artifact_uri`, 200 characters, and for an organisation, whose workspace name is 44
  characters, that layout would take 58 or more of them. On Postgres, which enforces the length,
  the store therefore takes an artifact root of at most 157 characters (`LONGEST_ARTIFACT_ROOT`)
  and refuses a longer one with `ValueError` before it writes anything. SQLite does not enforce
  the length and takes any root. A project whose
  experiment an earlier SDK release created keeps MLflow's layout, for its earlier runs and its
  new ones, so a data folder an earlier release wrote reads back unchanged.
- Trace search applies MLflow's own filters (run, session, user, tags, time), then the text filter
  in this process, so a text search reads every trace the other filters match. MLflow's filters
  have no "or", so a search for a run's traces is two searches, on the tag and on
  `mlflow.sourceRun`. A session search reads the same traces and groups the first of them in this
  process.
- An experiment named by a project's id that the store did not create, such as one MLflow tracing
  created for an application before the project existed, is not that project: creating the
  project is refused as a `conflict`.

The store contract runs on this binding with SQLite in every test run, and on Postgres when
`MARTIAN_TEST_POSTGRES_URL` names a database the tests may create schemas in. On both,
`tests/evals/test_store_mlflow.py` also records traces with MLflow tracing itself into the store's
database and reads back through the binding their sessions, turns and users, and the run of a
trace recorded inside a run's MLflow run. It starts a run with a 157-character artifact root,
reads runs an earlier release wrote in MLflow's layout, and checks that Postgres refuses a
longer root before writing. That module belongs to the
[MLflow compatibility suite](development.md#mlflow-upgrades), which also checks that a store
written by one MLflow release reads back the same on the next.

## The record store

MLflow keeps datasets, traces, assessments, registered scorers and review queues. The core also
needs records MLflow does not keep: runs with their lifecycle, case results, the scores it gates
on, scoring jobs, the human review chain, versioned suites, editable dataset drafts and the Traces
page's saved views.
`RecordStore` (`martian.evals.records`) is that interface. `SqlRecordStore` keeps the records in
SQLite or Postgres, with one Alembic history for both:

```python
from martian.evals.records.sql import SqlRecordStore

records = SqlRecordStore("sqlite:///evals/martian.db")
records = SqlRecordStore("postgresql+psycopg://user:password@host/db", schema="martian")
```

- The store migrates the database to the newest revision when it starts. Pass `migrate=False`
  where a separate job runs `martian.evals.records.sql.upgrade`. The version table is
  `martian_alembic_version`, so it can share a database with MLflow's tables.
- Each row keeps the whole model object as JSON, plus the columns queries filter and order on,
  so the tables use only portable types.
- Every statement filters on the organisation. On Postgres, row-level security filters again,
  against the organisation the store sets for each transaction, and applies to the tables' owner.
  A role that bypasses row-level security, such as a superuser, is held only by the store's own
  filter.
- Updates that move a run or scoring job on name the state they expect, and draft and view edits
  name the revision they expect. A review chain never forks: a review is replaced at most once.
- A suite version records who saved it and when (`Suite.created_by`, `created_at`). A draft can be
  renamed or described (`edit_draft`) and removed with its cases (`delete_draft`).
- A saved view (`SavedView` in `martian.evals.records`) is UI state, not evaluation content, so it
  is not part of the shared model: `save_view` creates one or, naming the revision it read,
  replaces it; `get_view`, `list_views` and `delete_view` read and remove them. Migration `0002`
  adds its table.
- Every change publishes an event (`martian.evals.records.events`) naming what changed.
  `InProcessEvents` delivers events within one process, which is enough for the local edition.
  The server delivers them between processes. A listener that falls too far behind gets
  `EventsLost` and reads everything again.
- Projects are not records: they are MLflow experiments. CI gate sessions stay with the server's
  CI gate.

`tests/evals/record_contract.py` is the behaviour; it runs on SQLite always and on Postgres when
`MARTIAN_TEST_POSTGRES_URL` is set. On Postgres it runs as an ordinary role that owns its schema,
so row-level security applies.

## The local edition (`martian ui`)

`martian ui` serves the core and the Evals UI from one process on this machine, with no login and
no other service, and opens the UI in the default browser. It needs `martian-sdk[evals]`, which
includes the `uvicorn` server, and works offline. `martian.evals.local` builds it. The wheel ships
the built UI; [the release guide](releasing.md#the-evals-ui-in-the-wheel) says which build and how
it is checked.

```bash
martian ui                                   # http://127.0.0.1:7600/
martian ui --port 7601 --data-dir ./evals-data --no-open
martian ui --ui-dir ../frontend/dist-evals   # another build, from `npm run build:evals` in the frontend
```

| Option | Default | What it sets |
| --- | --- | --- |
| `--port` | `7600` | The port to listen on. A port another process holds stops the command with exit code `2`. |
| `--data-dir`, or `MARTIAN_EVALS_HOME` | `~/.martian/evals` | The data folder |
| `--ui-dir`, or `MARTIAN_EVALS_UI_DIR` | The `ui` folder of the installed `martian` package, which the wheel ships | The built Evals UI: a folder holding `index.html` and `assets/` |
| `--open` / `--no-open` | `--open` | Whether to open the UI in the default browser (Python's `webbrowser`; `BROWSER` names another) |

- The principal is `SINGLE_USER` and the edition Community, so the bootstrap is the one
  [above](#bootstrap-response).
- The store is `MlflowStore` on SQLite (`mlflow.db` and `artifacts/` in the data folder), and the
  records are `SqlRecordStore` on SQLite (`martian.db`), all created on the first start with the
  default project (`open_stores` in `martian.evals.local`). `martian evals run` writes its runs
  into the same folder ([command-line runs](#command-line-runs)), and an application can record
  its traces there with MLflow tracing ([below](#traces-from-an-application)). What another
  process writes while the UI is open shows when a page reads again, since the UI gets no live
  event from it.
- The default judge model is a local setting, `judge_model` in `settings.json` in the data folder
  (`FileJudgeSettings`), set from the Judges page or `PUT /v1/evals/judge-model`. The file is
  written when the default is first set ([judge models](#judge-models)).
- The session call. The UI's API client first calls `POST /v1/auth/refresh` for an access token
  and a session. The local edition answers it with the fixed principal, so the UI needs no
  sign-in:

  ```json
  {
    "access_token": "local",
    "session": {
      "kind": "single_user",
      "user": {"id": "local", "email": null, "display_name": null},
      "organisation": null,
      "role": "owner",
      "platform_admin": false,
      "session_id": "local"
    }
  }
  ```

  `session` is the bootstrap's `principal` with a `session_id`. Nothing checks the token. The
  route belongs to the local edition, not to the core: the Martian server answers the same call
  from its own sign-in.
- Every path outside the API is a file of the built UI. The UI uses hash routes and relative
  asset paths, and calls `/v1` on its own origin. `/ui-version.json` names the UI's version and
  the frontend commit it was built from.
- The server listens on 127.0.0.1, and refuses with `400` a request whose `Host` is not
  `127.0.0.1` or `localhost`, so a web page cannot reach it through another name that resolves
  to this machine. There is no login: any program on this machine can call it. The container
  image listens on the container's interfaces instead, with the same check; see
  [below](#the-local-edition-in-a-container).
- If the UI folder holds no `index.html`, because an install has lost the packaged UI or
  `--ui-dir` names another folder, the command stops with exit code `2` and says so.

`tests/evals/test_local.py` runs the local edition on SQLite: the session call, the bootstrap,
the UI files, the `Host` check, the port, the browser and the command, which serves the packaged UI
when no `--ui-dir` is given, the container settings below with their warning, and an application's
traces ([below](#traces-from-an-application)).

### Traces from an application

An application instrumented with MLflow tracing (`@mlflow.trace`, `mlflow.start_span` or an MLflow
autolog integration) can record its traces straight into the data folder. `martian ui` then shows
them in **Traces**, and their conversations in **Sessions**. MLflow's own client writes them into
the store's database; no other service runs. Start `martian ui` once first, or record a
`martian evals run`, so the data folder and its default project exist. Then start the application
with these settings in its environment:

```bash
export MLFLOW_TRACKING_URI="sqlite:///$HOME/.martian/evals/mlflow.db"
export MLFLOW_ENABLE_WORKSPACES=true
export MLFLOW_WORKSPACE=local
export MLFLOW_EXPERIMENT_NAME=default
```

| Setting | What it does |
| --- | --- |
| `MLFLOW_TRACKING_URI` | The data folder's `mlflow.db`, as `sqlite:///` and the file's absolute path. With `--data-dir` or `MARTIAN_EVALS_HOME`, name that folder's `mlflow.db` |
| `MLFLOW_ENABLE_WORKSPACES`, `MLFLOW_WORKSPACE` | The store keeps the local user's objects in the MLflow workspace `local` ([the in-process binding](#the-in-process-binding)). Without the first, MLflow refuses to open the database; without the second, it refuses to write |
| `MLFLOW_EXPERIMENT_NAME` | The default project's experiment, named by the project's id, `default`. Another project's experiment is named by that project's id |

```python
import mlflow


@mlflow.trace
def answer(message: str, conversation: str, user: str) -> str:
    # A session and a user group the application's traces in Sessions.
    mlflow.update_current_trace(session_id=conversation, user=user)
    return "..."
```

- A trace's session and user are the ones [above](#sessions-and-users). A trace without a session
  is in Traces only.
- A trace recorded inside the application's own MLflow run (`mlflow.start_run()`) belongs to no
  run in the UI; one recorded inside a run's MLflow run belongs to that run
  ([the in-process binding](#the-in-process-binding)).
- Run the application with the MLflow release the SDK installs, such as in the same environment as
  `martian-sdk[evals]`. A newer MLflow migrates the database's tables, and the SDK then refuses it.
- MLflow tracing sends traces in the background and sends the rest when the process exits;
  `mlflow.flush_trace_async_logging()` sends them sooner.
- The database is a file on this machine, so this does not reach the container image or another
  machine. An OpenTelemetry endpoint for those is MorningHQ/martian-notes#149.

If the application names the experiment before the default project exists, MLflow creates the
experiment itself, with its artifacts under the application's working folder, and the store does
not take it as the project: `martian ui` and `martian evals run` then refuse the data folder with
exit code `2`. With the settings above, rename that experiment; the next start creates the
project, and the traces recorded so far stay in the renamed experiment, which the UI does not
show:

```bash
python -c 'from mlflow import MlflowClient; c = MlflowClient(); c.rename_experiment(c.get_experiment_by_name("default").experiment_id, "default-old")'
```

`tests/evals/test_local.py` runs such an application in a process of its own, with only these
settings, and reads its traces, sessions and spans back through the local edition's routes.

### The local edition in a container

The repository's `Dockerfile` builds the local edition as an image, for one user who runs it on a
small server rather than a laptop. It runs the same `martian ui`: the one wheel in `dist/`, with
the `evals` extra and its dependencies at the versions `uv.lock` pins, on `python:3.12-slim`. The
image installs that wheel and never builds the package. Both base images are pinned by digest.
The server runs as the user `martian` (uid 10001), with the data folder in `/data`, a volume, and
without opening a browser.

```bash
rm -rf dist && uv build --wheel
docker build -t martian-evals .
docker run -d --name martian-evals -p 127.0.0.1:7600:7600 -v martian-evals:/data martian-evals
```

A release builds the image from the wheel it publishes, checks that both hold the same SDK
version and Evals UI, and publishes it as `ghcr.io/morninghq/martian-evals:<version>`
([releasing](releasing.md#the-local-editions-image)). To run a published version instead of a
local build, give that name as the image, the last argument of `docker run`.

Then open http://127.0.0.1:7600/ on the server. The `martian-evals` volume holds `mlflow.db`,
`artifacts/`, `martian.db` and `settings.json`, so the data outlives `docker stop` and `docker start`, and a new
container started on the same volume. To keep the data in a folder of the server instead, mount
that folder at `/data`; the image's user, uid 10001, must be able to write to it.

**It has no login. Do not expose it publicly.** Anyone who can reach the port can read and change
everything in it. The command above publishes the port on the server's 127.0.0.1 only. From
another machine, forward the port over SSH (`ssh -L 7600:127.0.0.1:7600 <server>`) and open
http://127.0.0.1:7600/ there. Do not publish it on an address other machines can reach
(`-p 7600:7600` publishes it on every address of the server), and do not put it behind a proxy
that the internet can reach. Other containers on the same Docker network can reach it too. The
startup log carries the same warning:

```text
Martian Evals is listening on 0.0.0.0:7600 (data in /data). Press Ctrl+C to stop.
WARNING: Martian Evals has no login. Anyone who can reach this port can read and change everything in it. Do not expose it publicly: publish it on 127.0.0.1 only (docker run -p 127.0.0.1:7600:7600 ...) and reach it from another machine through an SSH tunnel.
```

Inside a container, the server must listen on the container's interfaces for Docker to publish the
port. Two environment variables, which `martian ui` reads and offers no flag for, set this up. The
image sets the first. Where both are unset, as for `martian ui` on a laptop, the server stays on
127.0.0.1 and answers only `127.0.0.1` and `localhost`.

| Setting | In the image | What it sets |
| --- | --- | --- |
| `MARTIAN_EVALS_BIND` | `0.0.0.0` | The IPv4 address to listen on. Unset, `127.0.0.1`. On any address but loopback, `martian ui` prints the warning above. A value that is not an IPv4 address stops the command with exit code `2`. |
| `MARTIAN_EVALS_ALLOWED_HOSTS` | Unset | Host names, separated by commas, that the `Host` check answers besides `127.0.0.1` and `localhost`: for a private network where users reach the server by its own name, as `-e MARTIAN_EVALS_ALLOWED_HOSTS=evals.internal`. A name with a port, a scheme or a wildcard stops the command with exit code `2`: the check compares the name alone, and a wildcard would switch it off. |

The `Host` check stays on in the container. A request through the published port carries the name
the browser used, `127.0.0.1` or `localhost`, and any other name is refused with `400`, as on a
laptop. The check keeps a web page from reaching the server through a name that resolves to it;
it does not keep out a program that reaches the port, which is why the port must stay private.

`tests/test_container_image.py` checks that the `Dockerfile` pins its base images, runs as a user
other than root and sets the variable `martian ui` reads, and that `.dockerignore` lets in every
file the build copies and no source to build the package from. CI does not build the image; the
release workflow does, and `scripts/check_image.py` checks it there.

## Command-line runs

`martian evals run` records each suite-file run in the local edition's data folder
(`martian.evals.run_record`), so `martian ui` shows it in Runs, the run detail, Compare and
Traces. The data folder is `--data-dir`, else `MARTIAN_EVALS_HOME`, else `~/.martian/evals`, as
for `martian ui`. The run writes through the store and the record store as `SINGLE_USER`, in the
default project:

| Object | What a run writes |
| --- | --- |
| `Project` | The default project, named `Default`, on the first run |
| `Run` | Source `local`, the suite's id and digest, its target and scorers, and the dataset's name (the data file's stem) and digest. The code is the suite's name and the run's number among that suite's runs, such as `evals-3`. `max_cases` is `--limit`, and `truncated` says the limit left cases out. `provenance` holds the suite's and the dataset's names and digests and the SDK version. In the store, an MLflow run in the default project's experiment |
| `CaseResult`, `Score` | One case result per `results.jsonl` record, by `from_results_record`, with the case's own expectations and its trace's latency. A check's score has source `code`; an LLM judge's has source `llm_judge` and the judge model as `source_id` |
| `Trace` | The trace MLflow's evaluation harness recorded for the case, with its spans, under a new `tr-` id, named after the run code and the case id, linked to the run, and carrying the case's scores as assessments. Its request is the case's input under `input_key` and its response the reply |

- The run is `running` while it grades, and its results and counts are written after each batch,
  so it shows while it runs. It is `completed` once every case has a result, with a verdict or
  not. A run that stops on an error is `failed`, with `error` naming the exception's type, and an
  interrupted one is `cancelled`.
- A resumed run is a new run. A case whose verdict it reuses from the output folder's
  `results.jsonl` has its result and scores but no trace, because the trace belongs to the run
  that graded the case; that result's trace route answers `409`.
- The data folder is opened, with its default project, before anything in the output folder
  changes. One that cannot be opened stops the run with exit code `2`.
- The output folder is written as before: `results.jsonl`, `summary.json` and `junit.xml` hold
  what they held. `run_suite` returns the summary with the recorded run under `run`, and the
  command prints the run's code and how to open it. The output folder's `mlflow.db` stays as the
  store of MLflow's evaluation harness, or the server `mlflow.tracking_uri` names gets those
  runs. The core does not read it.
- A suite whose target is instrumented with MLflow tracing shows the target's own spans inside
  each case's trace. The same application, run outside a suite, can record into the data folder
  too ([traces from an application](#traces-from-an-application)).

`tests/evals/test_runner.py` runs suite files and reads the recorded runs back through the local
edition's routes: Runs and the run detail against the contract, a case's trace and its spans,
trace search, Compare of two runs, a resumed run, a run that stops early, and a data folder that
cannot be opened.

## Not built yet

| Piece | Issue |
| --- | --- |
| The signed offline licence file | MorningHQ/martian-notes#131 |
| The server's REST binding of the store, moving its MLflow calls behind the interface | MorningHQ/martian-notes#105 |
| A durable worker | MorningHQ/martian-notes#139 |
| Mounting the core in the Martian server | MorningHQ/martian-notes#102 |

The objects the routes serve are the [shared eval data model](eval-model.md).
`tests/evals/test_server.py`, `test_entitlements.py`, `test_bootstrap.py`, `test_local.py`,
`test_read_routes.py`, `test_read_contract.py`, `test_write_routes.py`, `test_write_contract.py`,
`test_worker.py`, `test_worker_process.py`, `test_worker_targets.py`, `test_worker_judges.py` and
the store and record tests run the core without the backend. The `Evals core`
job in CI runs them with the model tests, on Postgres as well as SQLite.
