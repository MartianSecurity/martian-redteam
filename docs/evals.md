# Evals module

An MLflow-style regression gate for Martian dataset evals. It is the pattern from MLflow's
[Regression Testing and CI/CD](https://mlflow.org/docs/latest/genai/eval-monitor/regression-testing/)
page (`pytest` + `@mlflow.test` + `mlflow.genai.evaluate()`), pinned to the MLflow version that the
hosted server runs, with MartianBackend as the scorer and system of record. The CI job never
connects to MLflow.

```text
pytest ── load_dataset ──▶ Backend  GET  /v1/evals/ci/datasets/{name}   (pinned prompts + expected)
   │
   └── mlflow.genai.evaluate(...)   local sqlite store on the runner, your agent under test
   │
   └── assert_gate ── session end ─▶ Backend  POST /v1/evals/ci/runs    (outputs only)
                                       re-scores with exact match, stores the run, exports to MLflow
```

## Use

`martian.evals.evaluate(...)` provides the same MLflow prediction/scorer/result
contract with explicit scorer tracking. The combined [Python/CLI runner](sdk-and-ci.md)
records evals and YAML redteam in one Backend session.

1. An org admin creates an API key with the required evals/CI capabilities (`POST /v1/api-keys`) and stores it as `MARTIAN_API_TOKEN`.
2. Install `martian-sdk[evals]` (it pins `mlflow-skinny==3.16.1`, the hosted server's version) and write tests like
   [`examples/test_support_agent.py`](../examples/evals/test_support_agent.py).
3. Run `pytest -p martian.evals.plugin --martian-evals-gate tests/evals` (add `-n auto` with
   pytest-xdist), or copy [`workflows/eval-gate.example.yml`](../examples/combined-ci.yml).

Two kinds of regression test, any number per session, as in MLflow's regression-testing pattern:

| | `assert_gate(result, dataset)` | `assert_checks(result)` |
| --- | --- | --- |
| Data | Pinned dataset pulled from Backend (`load_dataset`) | Inline `data=[{"inputs": ...}]` |
| Scorers | `exact_match`, re-scored by Backend | Any scorer, including LLM judges (`Guidelines`-style `yes`/`no`), run on the runner |
| Pass rule | Strict by default: every row passes; declared threshold is labelled | Every row has every scorer, no error, no missing value, every value passes |
| Trust | `client_executed_server_scored` | `client_executed_client_scored` |

The session is recorded as one CI session holding every test (MLflow: one run per session) and
shown on Evals → Runs in the default project. A scorer that returns a number or a free-form label needs
`pass_if={"name": lambda v: v >= 0.8}`; anything else counts as not passing. Judge scorers need
their provider key on the runner (as in MLflow's own example); the kit never sees it.

| Variable | Required | Meaning |
| --- | --- | --- |
| `MARTIAN_API_URL` | yes | Backend origin, `https` (`http` only for localhost) |
| `MARTIAN_API_TOKEN` | yes | Scoped organisation API token |
| `MARTIAN_AGENT_ID` | yes | The onboarded agent the result is attributed to |
| `MARTIAN_EVALS_GIT_SHA` | no | Commit to attribute the run to; defaults to `GITHUB_SHA`, then `git rev-parse HEAD`. Set it to the PR head SHA in `pull_request` workflows |
| `MARTIAN_EVALS_THRESHOLD` | no | Declared relaxation of the strict default for dataset gates, 0–1. The server records it as `client_declared` and still reports `all_passed` |
| `MARTIAN_EVALS_MIN_COVERAGE` | no | Compatibility policy value; required scores must still be complete |
| `MARTIAN_EVALS_RUN_KEY` | no | Disambiguates matrix legs that share a run, agent and dataset |
| `MARTIAN_EVALS_STORE_DIR` | no | Choose the private local MLflow store; exclude it from shared artifacts; each xdist worker gets its own subdirectory |
| `MARTIAN_EVALS_OFFLINE=1`, `MARTIAN_EVALS_DATASET_FILE` | no | Gate locally from a saved pull; nothing is uploaded (checks need no file) |
| `MARTIAN_EVALS_ALLOW_PARTIAL=1` | no | Legacy partial local exploration; do not use for required CI coverage |
| `MARTIAN_EVALS_REDACT_INPUTS=1`, `MARTIAN_EVALS_REDACT_OUTPUTS=1` | no | Force digests even when content reporting is opted in (digests are the default) |

For shared redteam results, use `assert_redteam(assessment)` and
`martian evals run --require-redteam tests/evals`; see the
[shared session contract](sdk-and-ci.md#shared-pytest-sessions-and-result-provenance).

## What the gate enforces

The plugin does nothing unless `--martian-evals-gate` is passed. With it, the session fails when:

- a required variable is missing (a missing secret is a failed gate, never a skipped one) or
  `MLFLOW_TRACKING_URI` points at a server;
- any test is skipped (including a module skipped at collection) or deselected, or any
  `@mlflow.test` test passes without reaching `assert_gate`/`assert_checks`/`assert_redteam`, or an `evaluate()`
  result is never asserted (so `assert result.passed`, a swallowed error and an early return all
  fail);
- two scorers share a name (they share one result column and the last one wins), a scorer has no
  name, `scorers=[...]` leaves out a scorer `evaluate()` ran (`allow_subset=True` opts in), or the
  kit cannot tell which scorers ran (pass `scorers=[...]`);
- the result frame is missing or incomplete, a scorer the `evaluate()` call was given produced no
  column, a value is null, rows do not match the pulled items, or an agent call errored;
- the dataset has more usable items than Backend's 100-item cap (part of it cannot be gated), or
  `scorers` omits `exact_match`;
- an `assert_gate` failure was recorded, even if the test swallowed it;
- the upload fails after retries, the response is malformed, local and server scores disagree, or
  the server verdict is `failed`. A session `relaxed` verdict (a test passed only under a threshold
  you declared) does not fail the job and is shown as such in the console;
- a test exceeds Backend's bounds (100 rows per test, 20 scorers, 1,000 rows or about 3.8 MB per
  session): the kit says so locally instead of losing the whole session to a 422.

A test the kit cannot upload (an unusable frame, a failed run upload, a dataset gate that failed
locally on an extra scorer) is still recorded, as a failed placeholder, so the console cannot show a
green session for a red job.

With no threshold the server policy is strict: every pinned item must be evaluated and pass. A
threshold relaxes that on the uploader's own say-so; the verdict labels it `client_declared` and
reports `all_passed` regardless, so a relaxed pass is visible. Outputs over 20,000 characters are
uploaded as error rows. Only `exact_match` is re-scored by Backend; in a dataset gate, extra local
scorers must also pass locally but are not reflected in the server verdict.

## Why `assert_gate` and not `assert result.passed`

Historical qualification on MLflow 3.16.1 (the original Martian Evals
workspace archive, `evidence/ci-gate-20261012`):

- With `expectations` in the data, `result.passed` is `False` even when every scorer passed,
  because the expectation column is treated as a failing assertion.
- It is `True` when `result_df` is `None`, when a scorer column is missing, and when the store
  rejects trace or assessment writes. `result.metrics` can still look healthy.
- An untraced `predict_fn` with trace validation skipped yields `result_df = None`.

`assert_gate` reads the scorer columns itself and requires full row coverage.

## Token scope and rate limits

An org admin can bind a CI token to one agent and a list of dataset names when issuing it
(`agent_id`, `datasets`); a bound token can only upload for that agent and pull those datasets, and
reads back only its own agent's runs and sessions. Requests are limited per token (default 120 a
minute per API replica); the kit retries a `429` after its `Retry-After`, up to 30 seconds.

The plugin disables MLflow trace validation during the session to prevent an
extra prediction on the first item. Teardown restores the previous environment
setting.

## Parallel runs

With `pytest -n N` each xdist worker uses its own local store (two workers writing one sqlite
store collided on a unique constraint on MLflow 3.16.1) and hands its gate records to the
controller, which uploads once, so one session is recorded. Skips and worker errors are counted
on the controller.

## Trust and limits

The verdict is `client_executed_server_scored`. Backend holds `expected`, re-scores the uploaded
outputs and never accepts a client verdict, but it cannot prove the agent was really called or that
the git SHA is genuine. A token is organisation-wide unless its issuer binds it to an agent
and dataset names (see Token scope and rate limits above). Use it as a regression and visibility
signal. It is not a release attestation.

## Develop

```bash
uv sync --locked --extra evals
uv run --extra evals pytest -q tests/evals
```
