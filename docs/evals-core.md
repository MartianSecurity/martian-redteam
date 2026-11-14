# Evals core

`martian.evals.server` is the evals core: the `/v1/evals` API that the local edition and the
Martian server will both serve. It runs on its own, with no Martian backend, and needs only
`martian-sdk[evals]`.

The core never signs anyone in, never chooses a store and never decides an edition. An edition
builds it with `create_app` and supplies three things:

| Argument | What it is | Local edition | Martian server |
| --- | --- | --- | --- |
| `store` | An `EvalStore` (`martian.evals.store`) | `mlflow-skinny` in-process (planned) | Its MLflow server over REST (planned) |
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
- `EvalStore` has one method, `status`. `MemoryStore` implements it in memory and records which
  principals asked, for tests.
- `Entitlements` is an edition, its feature keys and its limits; `require_entitlement` refuses a
  route the edition leaves out. The core's own routes are all free so far.

## Not built yet

| Piece | Issue |
| --- | --- |
| The signed offline licence file | MorningHQ/martian-notes#131 |
| Read routes: runs, results, comparison, traces, datasets | MorningHQ/martian-notes#100 |
| Write routes: launch, scoring, datasets, judges, suites, reviews | MorningHQ/martian-notes#101 |
| The rest of the store interface and its MLflow bindings | MorningHQ/martian-notes#104, #106 |
| Worker entry points: one process per evaluation run | MorningHQ/martian-notes#139, #140 |
| Mounting the core in the Martian server | MorningHQ/martian-notes#102 |

The objects the routes will serve are the [shared eval data model](eval-model.md).
`tests/evals/test_server.py`, `tests/evals/test_entitlements.py` and
`tests/evals/test_bootstrap.py` run the core without the backend, and the `Evals core` job in CI runs them with the model tests.
