# Evals core

`martian.evals.server` is the evals core: the `/v1/evals` API that the local edition and the
Martian server will both serve. It runs on its own, with no Martian backend, and needs only
`martian-sdk[evals]`.

The core never signs anyone in, never chooses a store and never decides an edition. An edition
builds it with `create_app` and supplies three things:

| Argument | What it is | Local edition | Martian server |
| --- | --- | --- | --- |
| `store` | An `EvalStore` (`martian.evals.store`) | `mlflow-skinny` in-process (planned) | Its MLflow server over REST (planned) |
| `principal` | A FastAPI dependency returning the request's `Principal` | `lambda: SINGLE_USER`: one fixed user, no organisation | Its session lookup, as `organisation_principal(user, organisation, roles)` |
| `entitlements` | A function from a principal to its `Entitlements` | Community (the default) | The organisation's plan |

```python
from martian.evals.server import SINGLE_USER, create_app
from martian.evals.store import MemoryStore

app = create_app(store=MemoryStore(), principal=lambda: SINGLE_USER)
```

Routes read the injected values through `current_principal`, `current_entitlements` and
`current_store`. `principal` is a FastAPI dependency, so it can declare its own parameters (a
header, a cookie) and raise `401`.

## What exists

- `GET /v1/evals/status`: the store's readiness for the principal, in the shape of
  `EvalsStatusOut` in the server's frozen evals contract (`contracts/evals-api.json` in
  MorningHQ/MartianBackend).
- `EvalStore` (`martian.evals.store`) is the whole store interface. It has a method for every
  MLflow feature the evals code uses, and every method takes and returns the shared model's
  objects, never MLflow's:

  | Group | Methods |
  | --- | --- |
  | Projects | `create_project`, `get_project`, `list_projects`, `archive_project` |
  | Datasets | `create_dataset`, `list_datasets`, `get_dataset`, `find_dataset`, `read_cases` |
  | Run telemetry | `start_run`, `get_run`, `finish_run` |
  | Traces | `record_trace`, `search_traces`, `get_trace`, `set_trace_tag`, `link_traces` |
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
- `tests/evals/store_contract.py` is the behaviour every binding must have: idempotent creates,
  immutable dataset versions identified by content, archived projects refusing new work, trace
  search filters and paging, scores kept with their trace, versioned scorers, review queues,
  artifact names, and organisations never seeing each other. A binding's tests subclass
  `StoreContract`; `MemoryStore` passes it.
- `Entitlements` is an edition name and a set of feature keys. Nothing is gated on it yet.

## Not built yet

| Piece | Issue |
| --- | --- |
| The edition-to-feature map and a bootstrap response | MorningHQ/martian-notes#127, #128 |
| Read routes: runs, results, comparison, traces, datasets | MorningHQ/martian-notes#100 |
| Write routes: launch, scoring, datasets, judges, suites, reviews | MorningHQ/martian-notes#101 |
| The store's MLflow bindings: in-process on `mlflow-skinny`, and the server's REST binding | MorningHQ/martian-notes#106, #105 |
| Worker entry points: one process per evaluation run | MorningHQ/martian-notes#139, #140 |
| Mounting the core in the Martian server | MorningHQ/martian-notes#102 |

The objects the routes will serve are the [shared eval data model](eval-model.md).
`tests/evals/test_server.py` and `tests/evals/test_store.py` run the core without the backend,
and the `Evals core` job in CI runs them with the model tests.
