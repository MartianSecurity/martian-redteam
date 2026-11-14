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
- `EvalStore` has one method, `status`. `MemoryStore` implements it in memory and records which
  principals asked, for tests.
- `Entitlements` is an edition name and a set of feature keys. Nothing is gated on it yet.

## Not built yet

| Piece | Issue |
| --- | --- |
| The edition-to-feature map and a bootstrap response | MorningHQ/martian-notes#127, #128 |
| Read routes: runs, results, comparison, traces, datasets | MorningHQ/martian-notes#100 |
| Write routes: launch, scoring, datasets, judges, suites, reviews | MorningHQ/martian-notes#101 |
| The rest of the store interface and its MLflow bindings | MorningHQ/martian-notes#104, #106 |
| Worker entry points: one process per evaluation run | MorningHQ/martian-notes#139, #140 |
| Mounting the core in the Martian server | MorningHQ/martian-notes#102 |

The objects the routes will serve are the [shared eval data model](eval-model.md).
`tests/evals/test_server.py` runs the core without the backend, and the `Evals core` job in CI
runs it with the model tests.
