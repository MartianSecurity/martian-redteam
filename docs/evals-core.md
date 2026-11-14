# Evals core

`martian.evals.server` is the evals core: the `/v1/evals` API that the local edition and the
Martian server will both serve. It runs on its own, with no Martian backend, and needs only
`martian-sdk[evals]`.

The core never signs anyone in, never chooses a store and never decides an edition. An edition
builds it with `create_app` and supplies these:

| Argument | What it is | Local edition | Martian server |
| --- | --- | --- | --- |
| `store` | An `EvalStore` (`martian.evals.store`) | `MlflowStore`, `mlflow-skinny` in-process | Its MLflow server over REST (planned) |
| `records` | A `RecordStore` (`martian.evals.records`): runs, case results, scores, scoring jobs, reviews | `SqlRecordStore` on SQLite | `SqlRecordStore` on Postgres |
| `principal` | A FastAPI dependency returning the request's `Principal` | `lambda: SINGLE_USER`: one fixed user, no organisation | Its session lookup, as `organisation_principal(user, organisation, roles, ...)` with optional names |
| `entitlements` | A function from a principal to its `Entitlements` | Community (the default) | The organisation's plan, or an on-premises licence |
| `worker` | A `Worker` (`martian.evals.worker`) that runs what the write routes start | `InProcessWorker` with the agents it can call and, optionally, a judge | Its own worker ([the worker](#the-worker)) |

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
scoring job answers `503` ("This installation runs no evaluation worker"). Routes read the
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
same shape, `EvalsBootstrapOut` in the server's frozen evals contract (version 2 of
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
version 2 of the server's frozen contract (`contracts/evals-api.json` in MorningHQ/MartianBackend):

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
  (`NO_AGENT`). `agent_name` is `null`: the core has no agent registry. `dataset_id` is the run's
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
of the contract (`tests/evals/contracts/evals-api-v2.json`, unchanged from the server; see
`tests/evals/evals_contract.py`) and validates the responses against it, with UUIDs and
date-times checked.

## Write routes

The core serves the routes that launch, score and review runs and that edit datasets, suites,
judges, review queues, projects and saved views. Each is a port of the server's route onto the
store, the record store and the worker, and each operation is the one in version 2 of the frozen
contract:

| Routes | What they do | Module |
| --- | --- | --- |
| `POST /v1/evals`, `POST /{run_id}/cancel` | Admit a run of a registered agent over a published dataset and hand it to the worker; stop it | `launch` |
| `GET /scorers`, `POST /{run_id}/scores`, `POST /{run_id}/scores/{job_id}/cancel`, `POST /{run_id}/assessments/{assessment_id}/sync`, `GET /monitoring` | The scorers on offer; admit a scoring job of a completed run and hand it to the worker; stop it; keep a score on its trace; scoring jobs and their scorecards across runs | `scoring` |
| `GET`, `POST /suites` | Saved suites of built-in scorers and policies, a new version for each change | `suites` |
| `GET`, `POST /judges` | Registered judges, a new version for each registration | `suites` |
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
- **Runs.** A launch names a registered agent (`agent_id`) and a published dataset; an agent the
  worker does not know is `404` ("Agent not found"), a dataset not published is `422`. One run at
  a time per organisation (`409` "An eval run is already in progress"). Run codes count up from
  `EV-1001`. The worker runs the dataset's active cases up to `max_items`, skips a case with no
  usable prompt or expected text, calls the target once per case with no retry, grades each output
  by exact match and records a trace per called case.
- **Cancelling.** A queued run or scoring job is cancelled at once. A running one gets
  `cancel_requested_at` (`Run.cancel_requested_at`, `ScoringJob.cancel_requested_at`), and its
  worker stops before the next case: a run's remaining cases are skipped ("Stopped before this
  target request"), a job keeps the scores it made. Cancelling a finished run is `409`.
- **Scoring.** The scorers are the built-in ones by name (`exact_match`, `contains_expected`,
  `valid_json` and `response_length` are code; `correctness`, `safety` and `policy` are judges),
  registered judges pinned by name and version, or a saved suite version's scorers and policies
  (`suite_version_id`). Judge scorers need a worker with a judge (`Worker.judges`); without one
  they are refused (`422`) and `GET /scorers` shows them unavailable. One active scoring job per
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
operation of the contract but the ones above. `tests/evals/test_worker.py` covers the worker.

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

`InProcessWorker(store, records, targets={agent_id: call}, judge=None, target_timeout_seconds=120)`
runs each piece of work as a task of the API's event loop. A target is an async function from a
`Case` to its output (`TargetCall`); a judge is an async function from a `JudgeRequest` (the
prompt, expected text, output and the judge scorers to apply) to a `JudgeResult` (the model and a
verdict per scorer). `drain()` waits for the work submitted so far. Work is lost with the process:
a run interrupted by a restart stays `running` until the durable queue.

What builds on it:

| Issue | Change |
| --- | --- |
| MorningHQ/martian-notes#139 | A durable job queue and worker process: `submit` enqueues |
| MorningHQ/martian-notes#140 | One process per evaluation run, executing each `Work` |
| MorningHQ/martian-notes#137 | HTTP targets called from the worker, as another kind of target |
| MorningHQ/martian-notes#133 | The SDK judge as the worker's judge |
| MorningHQ/martian-notes#116 | Run and run again from the local UI |

## What exists

- `GET /v1/evals/status`: the store's readiness for the principal, in the shape of
  `EvalsStatusOut` in the server's frozen evals contract (`contracts/evals-api.json` in
  MorningHQ/MartianBackend).
- `GET /v1/evals/bootstrap`: the principal, edition, feature keys, capabilities and readiness,
  above.
- The [read routes](#read-routes) for projects, runs, results, comparison, traces and datasets.
- The [write routes](#write-routes): launch, scoring, Monitoring, suites, judges, reviews, review
  queues and questions, annotations, projects, saved views and dataset authoring, and
  [the worker](#the-worker) interface with its in-process implementation.
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
  local user gets `local`. Every call sets the workspace for its own thread through MLflow's
  per-request context. The binding never sets `MLFLOW_ENABLE_WORKSPACES` or `MLFLOW_WORKSPACE`,
  so other MLflow use in the same process, such as a local suite run, is unaffected.
- On Postgres, MLflow's tables live in their own schema (`mlflow` by default), so they can
  share a database with other tables.
- How the objects sit in MLflow:
  - A project is an experiment named by its id.
  - A dataset version is an evaluation dataset, whose records keep each case's id and position.
  - A run is an MLflow run, holding the run itself as `martian/run.json` and its counts as
    metrics.
  - Traces and spans are MLflow's. A trace or span id that is not in MLflow's form is kept in a
    tag, and MLflow's id is derived from it. A trace's session and user are MLflow tracing's
    metadata keys, [above](#sessions-and-users).
  - A score is a feedback assessment, and a registered scorer an MLflow scorer.
  - A review question is a label schema, and a review queue an MLflow review queue whose
    definition is an experiment tag.
- Run artifacts are files under the run's folder in the artifact root, which must be a local
  folder.
- Trace search applies MLflow's own filters (run, session, user, tags, time), then the text filter
  in this process, so a text search reads every trace the other filters match. A session search
  reads the same traces and groups the first of them in this process.

The store contract runs on this binding with SQLite in every test run, and on Postgres when
`MARTIAN_TEST_POSTGRES_URL` names a database the tests may create schemas in. On both,
`tests/evals/test_store_mlflow.py` also records traces with MLflow tracing itself into the store's
database and reads their sessions, turns and users back through the binding.

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

## Not built yet

| Piece | Issue |
| --- | --- |
| The signed offline licence file | MorningHQ/martian-notes#131 |
| The server's REST binding of the store, moving its MLflow calls behind the interface | MorningHQ/martian-notes#105 |
| A durable worker, and one process per evaluation run | MorningHQ/martian-notes#139, #140 |
| HTTP targets and the SDK judge in the worker | MorningHQ/martian-notes#137, #133 |
| Mounting the core in the Martian server | MorningHQ/martian-notes#102 |

The objects the routes serve are the [shared eval data model](eval-model.md).
`tests/evals/test_server.py`, `test_entitlements.py`, `test_bootstrap.py`, `test_read_routes.py`,
`test_read_contract.py`, `test_write_routes.py`, `test_write_contract.py`, `test_worker.py` and the
store and record tests run the core without the backend. The `Evals core` job in CI runs them with
the model tests, on Postgres as well as SQLite.
