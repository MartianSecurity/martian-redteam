# Evals core

`martian.evals.server` is the evals core: the `/v1/evals` API that the local edition and the
Martian server will both serve. It runs on its own, with no Martian backend, and needs only
`martian-sdk[evals]`.

The core never signs anyone in, never chooses a store and never decides an edition. An edition
builds it with `create_app` and supplies three things:

| Argument | What it is | Local edition | Martian server |
| --- | --- | --- | --- |
| `store` | An `EvalStore` (`martian.evals.store`) | `MlflowStore`, `mlflow-skinny` in-process on SQLite | Its MLflow server over REST (planned) |
| `principal` | A FastAPI dependency returning the request's `Principal` | `lambda: SINGLE_USER`: one fixed user, no organisation | Its session lookup, as `organisation_principal(user, organisation, roles, ...)` with optional names |
| `entitlements` | A function from a principal to its `Entitlements` | Community (the default) | The organisation's plan, or an on-premises licence |

```python
from martian.evals.server import SINGLE_USER, create_app
from martian.evals.store import MemoryStore

app = create_app(store=MemoryStore(), principal=lambda: SINGLE_USER)
```

Routes read the injected values through `current_principal`, `current_entitlements` and
`current_store`. `principal` is a FastAPI dependency, so it can declare its own parameters (a
header, a cookie) and raise `401`.

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

The refusal adds no parameters or responses to the route's OpenAPI operation. The Martian server
gates its own routes the same way until it mounts the core (MorningHQ/martian-notes#102); its copy
of the map is `app/entitlements.py` in MorningHQ/MartianBackend, and the two change together.

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

## What exists

- `GET /v1/evals/status`: the store's readiness for the principal, in the shape of
  `EvalsStatusOut` in the server's frozen evals contract (`contracts/evals-api.json` in
  MorningHQ/MartianBackend).
- `GET /v1/evals/bootstrap`: the principal, edition, feature keys, capabilities and readiness,
  above.
- `martian ui`, the local edition: the core and the Evals UI from one process, with no login. See
  [the local edition](#the-local-edition-martian-ui).
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
  route the edition leaves out. The core's own routes are all free so far.

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

The core's `POST /v1/evals/sessions/search` route on this method comes with
MorningHQ/martian-notes#100.

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
on, scoring jobs, the human review chain, versioned suites and editable dataset drafts.
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
- Updates that move a run or scoring job on name the state they expect, and draft edits name the
  revision they expect. A review chain never forks: a review is replaced at most once.
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
| `--port` | `7600` | The port on 127.0.0.1. A port another process holds stops the command with exit code `2`. |
| `--data-dir`, or `MARTIAN_EVALS_HOME` | `~/.martian/evals` | The data folder |
| `--ui-dir`, or `MARTIAN_EVALS_UI_DIR` | The `ui` folder of the installed `martian` package, which the wheel ships | The built Evals UI: a folder holding `index.html` and `assets/` |
| `--open` / `--no-open` | `--open` | Whether to open the UI in the default browser (Python's `webbrowser`; `BROWSER` names another) |

- The principal is `SINGLE_USER` and the edition Community, so the bootstrap is the one
  [above](#bootstrap-response).
- The store is `MlflowStore` on SQLite: `mlflow.db` and `artifacts/` in the data folder, created
  on the first start. The suite-file runner (`martian evals run`) keeps a `mlflow.db` of its own
  in its output folder. That is a plain MLflow store of MLflow's evaluation runs, which the core
  does not read, so `martian ui` does not open it. Writing command-line runs through the core's
  store, so that they show in the UI, is MorningHQ/martian-notes#114.
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
- The server listens on 127.0.0.1 only, and refuses with `400` a request whose `Host` is not
  `127.0.0.1` or `localhost`, so a web page cannot reach it through another name that resolves
  to this machine. There is no login: any program on this machine can call it.
- If the UI folder holds no `index.html`, because an install has lost the packaged UI or
  `--ui-dir` names another folder, the command stops with exit code `2` and says so.
- Until the read routes are ported (MorningHQ/martian-notes#100), the UI opens with the
  bootstrap and shows "Not Found" where a page reads projects, runs, datasets, traces or judges.
  `create_app` takes no record store yet, so the local edition does not open one.

`tests/evals/test_local.py` runs the local edition on SQLite: the session call, the bootstrap,
the UI files, the `Host` check, the port, the browser and the command, which serves the packaged UI
when no `--ui-dir` is given.

## Not built yet

| Piece | Issue |
| --- | --- |
| The signed offline licence file | MorningHQ/martian-notes#131 |
| Read routes: runs, results, comparison, traces, sessions, datasets | MorningHQ/martian-notes#100 |
| Write routes: launch, scoring, datasets, judges, suites, reviews | MorningHQ/martian-notes#101 |
| The server's REST binding of the store, moving its MLflow calls behind the interface | MorningHQ/martian-notes#105 |
| Worker entry points: one process per evaluation run | MorningHQ/martian-notes#139, #140 |
| Mounting the core in the Martian server | MorningHQ/martian-notes#102 |
| Command-line suite runs written through the core's store, so `martian ui` shows them | MorningHQ/martian-notes#114 |

The objects the routes will serve are the [shared eval data model](eval-model.md).
`tests/evals/test_server.py`, `test_entitlements.py`, `test_bootstrap.py`, `test_local.py` and the
store and record tests run the core without the backend. The `Evals core` job in CI runs them with
the model tests, on Postgres as well as SQLite.
