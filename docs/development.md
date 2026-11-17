# Development

The distribution is now `martian-sdk`; include `--extra evals` when syncing
and running the SDK suite. The migrated `tests/evals` suite exercises real local
MLflow and loopback Backend/judge fixtures. The existing redteam tests remain
external-network-free. See [SDK and CI](sdk-and-ci.md).

## Layout

Editable install from the repo root (`uv sync`). Packages live under `src/martian`, `src/martian_redteam`, and `src/martian_evals_kit` (hatchling src layout).

| Path | Role |
| --- | --- |
| `src/martian/config.py` | Immutable origin/token and separate CI attribution |
| `src/martian/redteam/client.py` | Typed Backend operations, status recovery and total deadline |
| `src/martian/evals/runner.py` | Suite file, JSONL/CSV cases, resumable local results, JUnit |
| `src/martian/evals/stats.py` | Confidence intervals for pass rates and mean scores, grouped or not ([evals](evals.md#pass-rates-and-confidence-intervals)), and the paired tests and label of a comparison of two runs ([comparing two runs](evals.md#comparing-two-runs)) |
| `src/martian/evals/agreement.py` | A judge version's agreement with people's labels: agreement, Cohen's kappa, false-pass and false-fail rates, and the bias and concordance of numeric scores ([evals](evals.md#judge-agreement-with-human-labels)); and people's agreement with each other per review queue and question, with Cohen's or Fleiss' kappa and Lin's concordance or the intraclass correlation ([evals](evals.md#agreement-between-reviewers)) |
| `src/martian/evals/panel.py` | A panel of judges: the strict-majority rule, the flat metadata of every member's vote, and `judge_panel` for the SDK judges ([evals](evals.md#a-panel-of-judges)) |
| `src/martian/evals/calibration.py` | A judge version's calibration: whether its newest one checks the model it runs on and what it means (`judge_definition`), what a judge score names as the calibration that applied, and `save_calibration` and `recheck_calibration` for the local data folder ([evals](evals.md#calibration)) |
| `src/martian/evals/model/` | Shared eval data model and the results-record mapping |
| `src/martian/evals/records/` | Record store on SQLite or Postgres, Alembic migrations (`migrations/`), change events |
| `src/martian/evals/server/` | Evals core: `create_app` with injected store, record store, principal and worker, the edition-to-entitlement map and `require_entitlement`, the capability table and `require_capability`, `/v1/evals/status`, `/v1/evals/bootstrap`, the read routes (`runs.py`, `traces.py`, `datasets.py`, and the core's own `agreement.py` and `calibration.py`, which also saves a calibration and asks for its re-check), the write routes (`launch.py`, `scoring.py`, `reviews.py`, `suites.py`, `projects.py`, `views.py`, `drafts.py`, bodies in `write_schemas.py`), OpenTelemetry's `POST /v1/traces` (`otlp.py`), and the core's own monitor and threshold routes (`monitors.py`, `thresholds.py`), shaped by `projection.py` and `quality.py` |
| `src/martian/evals/worker/` | The worker interface the write routes hand runs, scoring jobs and re-checks to, the work itself (`execute.py`, and `recheck.py` for a calibration's re-check), the console's built-in scorers (`scorers.py`), the per-case hooks and installed checks an edition adds (`hooks.py`), the targets it calls (`targets.py`: HTTP targets called directly, registered agents through martian-agents), the judges it grades with (`judges.py`: the SDK judge, and martian-agents as an optional executor), the one-organisation in-process worker (`inprocess.py`), and one process per run (`process.py`, started as `python -m martian.evals.worker`) |
| `src/martian/evals/store/` | Store interface, the in-memory store and the in-process `mlflow-skinny` binding (`mlflow_local.py`) |
| `src/martian/evals/thresholds.py`, `alerts.py` | Thresholds on monitor scores judged on their 95% intervals, with the state machine that raises one alert per breach and recovery, and the alerts' delivery by signed webhook and SMTP ([evals core](evals-core.md#thresholds-and-alerts)) |
| `src/martian/evals/trace_conventions.py` | The versioned table that maps OpenTelemetry GenAI, OpenInference and MLflow span attributes onto the trace fields ([evals core](evals-core.md#trace-conventions)) |
| `src/martian/evals/local.py` | The local edition behind `martian ui`: the data folder and its stores, the core on SQLite, the session call and the built UI on 127.0.0.1, or on the container's interfaces in the image |
| `src/martian/evals/local_targets.py`, `local_runs.py` | The local edition's targets and their keys, and its worker, launch and run again, each run queued in the data folder and run in a process of its own |
| `src/martian/evals/run_record.py` | A suite-file run recorded in the local edition's data folder: run, case results, scores and traces through the store and the record store |
| `src/martian/evals/inspect_import.py` | Inspect AI `.eval` and `.json` logs read against Inspect's log schema and recorded in the data folder as runs, the same way |
| `src/martian/evals/promptfoo_import.py`, `promptfoo_convert.py` | promptfoo results files recorded in the data folder as runs, one per provider, through the Inspect importer's `record_imported_run`; promptfoo configs written as suite files with what does not convert listed. `examples/promptfoo/` holds a hand-written config, its stand-in target and results, and `tests/evals/promptfoo/` results promptfoo 0.125.1 wrote for two providers |
| `src/martian/evals/data_export.py` | An organisation's data exported through the store and the record store as JSON Lines files with a manifest, resumed after an interruption, and imported into a fresh install ([data export](data-export.md)) |
| `src/martian/evals/mlflow_import.py` | An MLflow tracking store's schema revision checked, the store read without writing, and its experiments, traces, datasets, assessments and evaluation runs copied into an organisation |
| `Dockerfile` | The local edition as an image for one user on a small server: the wheel in `dist/` with the `evals` extra and the dependencies `uv.lock` pins; `scripts/check_image.py` checks a release's image holds the wheel's SDK and Evals UI ([evals core](evals-core.md#the-local-edition-in-a-container), [releasing](releasing.md#the-local-editions-image)) |
| `src/martian/ui/` | The built Evals UI the wheel ships, copied in by `scripts/vendor_ui.py` and pinned in `pyproject.toml` ([releasing](releasing.md#the-evals-ui-in-the-wheel)) |
| `scripts/sbom.py`, `scripts/licence-policy.json` | The release's CycloneDX bills of materials for the wheel and the image, and the licence check against the allowed list ([releasing](releasing.md#bills-of-materials-and-the-licence-check)) |
| `src/martian/evals/checks.py` | The deterministic checks; `_json_schema.py` holds the JSON Schema subset `json_schema` applies, `_numeric.py` the exact reading of numbers, paths and tables the numeric and table checks use, `_policy.py` the patterns, checksums and refusal reading of the policy checks, and `_tool_calls.py` the reading of an agent's tool calls, turn by turn, from its reply or its trace, and their matching; the suite file runner checks options with all four. `_custom.py` holds the rules of a custom Python check, shared by `python_check`, the runner's `type: python`, the worker's installed scorers and its installed checks, and the `required` option of an installed check |
| `src/martian/evals/_judge_transport.py` | Judge model call, error codes and rate-limit retry |
| `src/martian/evals/cache.py` | The cache of model calls a suite-file run replays: keys without credentials, entries on disk, an attempt number per identical request ([evals](evals.md#a-cache-of-model-calls)) |
| `src/martian/evals/_mlflow.py` | Every MLflow internal the SDK uses, and the span calls of a function target in a process of its own; `tests/evals/test_mlflow_private.py` names any internal a new MLflow release removes. The one exception is that process itself (`_target_process.py`), which never imports the evals package: it imports the two internals its tracing needs, which `_mlflow.py` lists and the guard allows in that file only (`OWN_IMPORTS`, with the reason) |
| `src/martian_redteam/protocol.py` | Wait-body JSON, envelope unwrap, Retry-After |
| `src/martian_redteam/client.py` | httpx client, retries, schema errors |
| `src/martian_redteam/orchestrator.py` | Probe loop and summaries |
| `src/martian_redteam/catalog/` | IDs only |
| `tests/fixtures/` | Recorded `/runs/wait` graph state; Inspect AI logs and the task that wrote them (`inspect/`); the script that writes a test MLflow tracking store with MLflow itself (`mlflow/write_store.py`) |
| `docs/` | Architecture and contracts |
| `cla/` | The individual and corporate Contributor License Agreements with MartianSecurity, and the corporations that signed (`corporations.md`); [CONTRIBUTING.md](../CONTRIBUTING.md#contributor-license-agreement) describes signing |

## Commands

```bash
uv sync --locked --extra evals
uv run --extra evals ruff format --check src tests examples scripts
uv run --extra evals ruff check src tests examples scripts
uv run --extra mcp --extra evals mypy src
uv run --extra evals pytest -q -n auto
uv run --extra evals python -m compileall -q src
```

This is the canonical local Python check block; keep it aligned with `.github/workflows/ci.yml`. Start with affected tests and use the full required gates before publication. Ruff formatting above is read-only; use write mode only for task files. `-n auto` runs the suite on one pytest-xdist worker per CPU; see [Parallel runs](#parallel-runs).

Optional local coverage (CI always produces XML + JUnit; pytest-cov combines the workers' data):

```bash
uv run --extra evals pytest -q -n auto --cov=martian_redteam --cov-report=term-missing --cov-report=xml --junitxml=junit.xml
```

Ruff: `E,F,I,UP,B,SIM`, Python 3.12, line length 120. Mypy: `strict` on both SDK namespaces.

## This repository’s CI

`.github/workflows/ci.yml` is split so branch protection can require a single aggregator:

| Job | Command | Python |
| --- | --- | --- |
| `lint` | `uv sync --locked --extra evals`, Ruff format/check with Evals extras and `scripts` | 3.12 |
| `types` | `uv sync --locked --extra evals`, `uv run --extra mcp --extra evals mypy src`, compileall | 3.12 |
| `test` | `uv sync --locked --extra evals`, pytest `-n auto` with coverage + JUnit | **3.12 and 3.13** (matrix) |
| `evals-core` | The evals core's tests with `-n auto`, with the stores on Postgres too | 3.12 |
| `mlflow-compat` | `scripts/mlflow_compat.py` on the pinned MLflow, then on the newest release (reported only); see [MLflow upgrades](#mlflow-upgrades) | 3.12 |
| `packaging` | `uv build`, `scripts/check_release.py --install` (clean installs, the pinned Evals UI served by `martian ui`), `scripts/sbom.py wheel` (the wheel's bill of materials and the licence check), throwaway venv install, `validate examples/config.yaml` | 3.12 |
| `action-smoke` | `uses: ./.github/actions/validate`, then `./.github/actions/evals` on the passing and the failing sample suite; the failing one must fail its step with two failed cases | 3.12 |
| `ci` | Succeeds only if every job above succeeded | — |

Require the **`ci`** check on `main`, not the individual matrix cells. Format is `--check` (no rewrite).

`test` uploads `coverage.xml` and `junit.xml` as artifacts (including on failure). It does **not** call Martian Agents, does **not** set `MARTIAN_API_TOKEN` or `OPENAI_API_KEY`, and does not run `martian redteam run`.

## Contributor License Agreement check

`.github/workflows/cla.yml` asks the author of every commit in a pull request to sign the
[individual agreement](../cla/individual.md) and records the signatures;
[CONTRIBUTING.md](../CONTRIBUTING.md#how-signing-works) describes it from the contributor's side.
It runs `contributor-assistant/github-action` v2.6.1, pinned to
`ca4a40a7d1004f18d9960b404b97e5f30a505a08`. That repository is archived and read-only, so the
pinned code cannot change and no later release will come. The action declares `node20`; runners
no longer have Node 20 and run JavaScript actions on Node 24.

- **Events.** `pull_request_target` (opened, synchronize, closed) and `issue_comment` (created).
  A comment starts the job only on a pull request, and only when it is the signature line or
  `recheck`. On `closed` the action locks the pull request's conversation instead of checking.
- **Default branch only.** Both events run the workflow file from `main`, whatever the pull
  request's base or changes, so the check exists once this file is on `main`, and a pull request
  cannot change what it runs.
- **Never the pull request's code.** The job has a write token, so it has no checkout, no script
  and no other action: the pinned action reads the pull request's commits and comments through the
  API and writes its comment and the signatures file. Keep it that way.
- **Permissions.** None at workflow level. The job has `contents: write` for the signatures file,
  `pull-requests: write` to comment and lock, and `actions: write` to re-run the failed check after
  a signature. It needs no `statuses` permission and no personal access token.
- **Signatures.** `signatures/cla.json` on the `cla-signatures` branch, written with the workflow
  token. Branch protection or a ruleset on that branch blocks the writes.
- **Allowlist.** `kloudd`, `dependabot[bot]` and `github-actions[bot]`, by exact name. The
  action's `*` pattern is unanchored, so do not use one. The action matches a commit's GitHub
  login, or its git author name when the commit's e-mail is linked to no account, so a commit with
  an unlinked e-mail and an allowlisted name skips the check: check that such commits on a pull
  request are linked to the account (GitHub shows an unlinked author without a profile link).
- **Agreement versions.** A signature records when it was made, not which text it signed; the Git
  history of `cla/individual.md` gives the text at that time. To have everyone sign a changed
  agreement, point `path-to-signatures` at a new file.

Before the first outside pull request, a maintainer sets up two things once:

1. **The signatures branch, with an empty file.** v2.6.1 does not create a missing file: it fails
   with `Could not retrieve repository contents. Status: 404`
   ([contributor-assistant/github-action#155](https://github.com/contributor-assistant/github-action/issues/155)).

   ```bash
   git switch --orphan cla-signatures
   mkdir -p signatures
   printf '{\n  "signedContributors": []\n}\n' > signatures/cla.json
   git add signatures/cla.json
   git commit -m "Start the CLA signatures file"
   git push origin cla-signatures
   git switch main
   ```

2. **An Actions policy that allows `pull_request_target`.** GitHub blocks `pull_request_target`
   in public repositories unless an Actions policy allows it (enforced from 2 December 2026). In the
   repository's **Settings**, **Actions**, **Policies**, add an active policy that targets
   `.github/workflows/cla.yml` and whose event rule allows `pull_request_target` and
   `issue_comment`.

## Testing this library

The product is a CI/CD SDK. GitHub Actions is the harness; this package is the subject. Do not red-team `martian_agents` as if it were the customer app.

| Layer | Where | Secrets | Required on PRs |
| --- | --- | --- | --- |
| 1. Library | `pytest` + typed operations/legacy recorded fixtures | none | yes |
| 2. Packaging / Action | wheel install + `validate`; composite `.github/actions/validate`; composite `.github/actions/evals` on `examples/github-action` (comments with the workflow token, `pull-requests: write`) | none | yes |
| 3. Live dogfood | `martian redteam run` / root `action.yml` / `red-team.yml` against staging graphs | `MARTIAN_API_TOKEN` | **no** (dispatch + environment, after coordinated single-key service qualification) |

Layer 2 uses a nested validate composite so the customer gate Action (`action.yml`) stays **run-only**. That avoids a `command: validate` switch that could later be forgotten on `pull_request` once a staging key exists.

Third-party actions are SHA-pinned. Concurrency cancels in-progress runs on the same ref.

Customer red-team workflows (reusable workflow, composite action, `validate` / `run -c -a`) and the evals pull-request action are documented in [gate and CI](gate-and-ci.md). They are not this repo’s unit-test pipeline.

## Dependabot

`.github/dependabot.yml` opens **weekly** PRs for:

- `package-ecosystem: uv` — `pyproject.toml` / `uv.lock`
- `package-ecosystem: github-actions` — action SHAs used by CI, the reusable workflow, and the composite action

Review action bumps by comparing SHAs. Do not merge a bump that reverts a pin to a floating tag (`@v4`, `@main`).

## Tests

- **No live network.** `respx` intercepts `https://agents.test.example/runs/wait`.
- Fixtures in `tests/conftest.py` plus JSON under `tests/fixtures/`.
- **No test touches the developer's `~/.martian`.** An autouse, session-scoped fixture in
  `tests/conftest.py` points `HOME` and `MARTIAN_EVALS_HOME` at a temporary folder for the whole
  session, so the local edition's default data folder (`~/.martian/evals`) and MLflow's
  `~/.config` resolve there, and every subprocess a test starts inherits both. A test that needs
  another home sets its own (`test_no_outbound.py` starts its processes in a clean environment
  with a new `HOME`). At the end of the session the fixture fails the run if a test reached the
  default data folder (the temporary home gained a `.martian`). In CI (`CI` set) it also fails if
  the real `~/.martian` or `~/.martian/evals` changed; it checks the real folders by `stat` only,
  never reads or lists them, and never deletes anything. Outside CI that second check is skipped,
  because `martian ui` or another checkout's tests may change the folder during the session.
- The evals store contract (`tests/evals/store_contract.py`) runs on every store binding. The
  in-process `mlflow-skinny` binding runs it on SQLite always, and on Postgres when
  `MARTIAN_TEST_POSTGRES_URL` is set (for example
  `postgresql+psycopg://user:password@127.0.0.1:5432/evals`, with `uv sync --extra postgres`).
  Each Postgres test creates and drops its own schema.
- The record store contract (`tests/evals/record_contract.py`) runs the same way. On Postgres it
  connects as an ordinary role that owns its schema, so row-level security is enforced; the URL's
  user must be able to create roles.
- `scripts/trace_store_load.py` is the in-process store's trace load test. It is not part of the
  suite: `tests/evals/test_trace_store_load.py` only checks its arguments and runs every phase on
  a few traces on SQLite. Its results and how to re-run it are in
  [trace store limits](trace-store-limits.md).
- `tests/evals/test_no_outbound.py` runs `martian evals run`, `martian ui` and the core with a
  process worker as subprocesses under `tests/evals/outbound_guard/sitecustomize.py`, which every
  Python process with that folder on `PYTHONPATH` loads at start. The guard refuses and records
  every connection and name lookup for an address other than loopback, and records MLflow's
  telemetry client if it starts; the test fails on any record. Its processes get a clean
  environment, because MLflow turns its telemetry off by itself under CI or pytest variables
  ([telemetry](security-and-data.md#telemetry)). The child processes import
  `tests/evals/no_outbound_fixture.py` and `tests/evals/judged_call_fixture.py` (one judged call at a
  loopback endpoint, with and without an address policy), so run it from the repository root.
- `tests/evals/test_judge_address_pin.py` covers a judge's address policy
  (`martian.evals._pinned_address`): loopback servers on `127.0.0.1` and `127.0.0.2` stand for the
  public and the internal address of one name, whose lookups the test scripts, and count the
  connections they accept. Its TLS tests generate a certificate with `cryptography`, which the
  `evals` extra brings in through MLflow.
- `tests/test_evals_pr_comment.py` covers the evals action's comment script (`scripts/evals_pr_comment.py`): it runs both sample suites in `examples/github-action`, the failing one again with the passing one's output folder as its baseline, and the matrix example `examples/evals/matrix`, requires the rendered bodies to equal `examples/github-action/comments/`, and drives find-and-edit against a loopback fake of the GitHub issue-comments API. Regenerate the four sample comments when the comment format changes. `tests/evals/test_baseline.py` covers `--baseline` (`martian.evals.baseline`): what `verdicts.json` keeps and leaves out, a seeded regression beyond noise that fails the gate, a few broken cases within noise that do not, one case that newly fails a security check, which fails it on its own, the failed cases with no result to compare with that do, and a check's digest, which leaves out the scope and panel it does not set and counts a panel's members, not `judge.model`. `tests/evals/test_matrix.py` covers a suite's matrix (`martian.evals.matrix`): one result and one recorded run per combination, the values in a URL target's body and a function target's arguments, resume and repeats per combination, each combination compared with its own baseline, and the matrices refused before any case. `tests/evals/test_cache.py` covers the cache of model calls (`martian.evals.cache`) against one counting loopback server that stands in for a URL target and an OpenAI-compatible judge: a second identical run makes no call to either, a changed prompt misses, failed calls are never stored, each attempt of a repeated case keeps its own entry, credentials stay out of the key and the entries, function targets are cached only when the suite says so, entries expire, a replayed judgment spends none of `max_judge_tokens`, nor does a panel whose members were all replayed, each member cached by its own model, and a run stopped at a limit still records its use of the cache, and the summary, the printed line, the pull-request comment and the recorded run's provenance say the run used the cache. The action's cache steps run only on GitHub.
- `tests/test_sbom.py` covers the bills of materials and the licence check (`scripts/sbom.py`) on probe fixtures: a GPL, AGPL or unnamed licence in a dependency or in the UI's bundled packages fails, conclusions and exceptions apply only to the licence they name, the Evals UI is recorded as first-party Apache-2.0 and `NOTICE` attributes it, and the packages of the shipped UI pass the policy. It does not install the wheel or start the image.
- `tests/evals/test_policy_checks.py` measures each policy check on its labelled set in `tests/evals/policy_sets/` and fails unless the error-rate table in `docs/evals.md` gives the same numbers; when a check or its set changes, copy the row the failure prints into that table. Every value in the sets is synthetic, and each secret in `no_secrets.jsonl` has one character written as a JSON escape so the file holds no secret shape.
- `tests/evals/test_grounding_judges.py` drives the grounding judges with a fake provider and
  checks the shape of their labelled set, `tests/evals/benchmarks/grounding_judges.jsonl`.
  Scoring the judges on that set needs a real model, so it is not part of CI:
  `scripts/benchmark_grounding_judges.py --model openai:/<model> --out results.jsonl` (with the
  provider's key and address in the environment) makes one call per case, about 75 in all, and
  prints each judge's false pass and false fail rates for [the evals guide](evals.md#grounding-and-retrieval-judges).
- `tests/evals/test_conversation_judges.py` drives the conversation judges with a fake provider:
  their flat verdicts, the turns and quotes checked against the conversation, the suite file's
  refusals, and a scripted and a simulated conversation run through `martian evals run`, with the
  verdicts in `results.jsonl`, the summary, the pull-request comment and the core's routes. It also
  checks the shape of their labelled set, `tests/evals/benchmarks/conversation_judges.jsonl`, which
  `scripts/benchmark_conversation_judges.py --model openai:/<model> --out results.jsonl` scores with
  a real model, one call per case, 30 in all, outside CI, for
  [the evals guide](evals.md#conversation-judges).
- CLI tests use Typer’s `CliRunner` for `--version`, `catalog`, and `validate` (`tests/test_cli.py`). `tests/test_cli_run.py` drives `run` end to end with `respx` in place of the hosted API and asserts the reports are written and the exit code is `0` on a pass and `1` on a gated failure or an infrastructure error. They never need a real key or OpenAI.
- `tests/test_cla_workflow.py` reads `.github/workflows/cla.yml`: its two events and the comments
  that start it, one step that is the SHA-pinned action with no checkout or script, its
  permissions, the signatures branch and the exact allowlist, and that the agreements the bot links
  are in `cla/` and give its signature line ([the check](#contributor-license-agreement-check)).

Keep typed Backend operation tests separate from retained legacy envelope/heartbeat cases. Real model/API qualification is required after contract changes; loopback fixtures do not qualify it.

### Parallel runs

CI and the local command run the suite on pytest-xdist with `-n auto`, one worker per CPU. Any two
tests may run at the same time, in two workers or in two checkouts sharing one Postgres database,
so a test keeps its state to itself:

- Files go under `tmp_path`, which pytest makes per test and per worker. Tests that start pytest in
  a subprocess give it its own temporary folder and `MARTIAN_EVALS_STORE_DIR`.
- Postgres tests use randomly named schemas and roles, drop them afterwards, and do not hold
  connections between tests. MLflow keeps an engine for each database URL until the process exits,
  so the Postgres MLflow store tests run it without a connection pool
  (`MLFLOW_SQLALCHEMYSTORE_POOLCLASS=NullPool`); otherwise every test's pool stays open and a full
  run uses up the server's connection limit. A test that reads the database catalog allows for
  other tests' schemas.
- Loopback servers bind port 0. A test that needs a port nothing answers on keeps the socket bound
  while it runs, so no other worker's server is given that port.
- The MLflow tracking URI and active experiment, `martian.evals.state.STATE` and environment
  variables are process-wide, so each worker has its own. Fixtures that change them restore them
  afterwards.
- A busy machine runs every test slower, so a test does not sleep for a lease to run out or a
  retry to come due in its own process: it moves the clock the code reads (`_now` in
  `martian.evals.records.queue`, `martian.evals.alerts` and the other modules that have one). It
  waits for the event it asserts rather than sleeping a fixed time, and holds a hung target until
  the run has ended rather than bounding how long the run took. Waiting on the wall clock is left
  to what only it can show, such as another process's lease or a monitor's scoring delay, with a
  wide margin.

For debugging (`--pdb`, `-s`) or a failure that appears only in parallel, run serially by leaving
out `-n auto` or passing `-n 0`. Each worker imports MLflow, and some tests start further pytest
processes, so on a machine short of memory give a fixed count such as `-n 4`.

Wall-clock time of the full suite (849 tests) with Postgres on a 12-CPU workstation that was also
running other test suites:

| Command | Workers | Time |
| --- | --- | --- |
| `pytest -q` | 1 | about 15.5 minutes (940 s) |
| `pytest -q -n auto` | 12 | about 4.5 minutes (255 to 277 s over three runs) |

The longest single tests start pytest in a subprocess and take 20 to 40 seconds each in a parallel
run. On GitHub's runners `-n auto` starts one worker per runner CPU.

## MLflow upgrades

`mlflow-skinny` is pinned exactly in the `evals` extra. The MLflow compatibility suite is every
test module that runs MLflow itself: the store binding (`test_store_mlflow.py`) and its errors
(`test_store_errors.py`), the judge transport and the judges built on it, the private-import
guard (`test_mlflow_private.py`), the pytest kit, the suite runner, the MLflow store copy
(`test_mlflow_import.py`, which has the release under test write the store it copies), and the
setting that turns MLflow's telemetry off (`test_no_outbound.py`).
`tests/evals/test_mlflow_compat.py` adds the MLflow behaviour the store contract does not pin: MLflow refusing a trace id that would leave its trace folder,
the store giving MLflow only ids of MLflow's own form, and the upgrade path.
`scripts/mlflow_compat.py` lists the modules and runs them on any release:

```bash
python3 scripts/mlflow_compat.py pinned next        # the pin, then the newest release on PyPI
python3 scripts/mlflow_compat.py pinned 3.19.0rc0   # or any exact version
```

Each release gets its own environment under `.venv-mlflow/`, built from `uv.lock` with only
`mlflow-skinny` replaced, so the project's `.venv` is untouched. The first release writes a store
(SQLite, and Postgres when `MARTIAN_TEST_POSTGRES_URL` is set); each later one must open it,
which migrates MLflow's schema, and read back the same content. The `MLflow compatibility` CI job
runs the pinned release, which is required, then the newest release on PyPI, which is reported
but does not fail CI, and carries the store from one step to the other. While the pin is the
newest release, the second step has nothing to test and says so. A Dependabot pull request that
moves the pin runs the required step on the new version; the upgrade from the old pin is the one
the second step checked on `main` before.

To move the pin, run the suite on the candidate, change `mlflow-skinny==` in `pyproject.toml`, run
`uv lock`, and update the version in [SDK and CI](sdk-and-ci.md) and
[release preparation](releasing.md). The hosted MLflow server's version is set separately in
MartianSecurity/martian-evals.

## Adding a vulnerability

1. Add a factory in `catalog/vulnerabilities.py` with `id`, `label`, `allowed_types`, and `gated`.
2. Register it in `VULNERABILITIES` (PascalCase key). Lookup aliases are generated from the name and `id`.
3. Add YAML/Python examples and a catalog test.
4. **Do not** put rubric prose in the SDK. Add the rubric to the evaluator graph in martian-agents and extend that graph’s `context_schema` if needed.
5. Bump `CATALOG_VERSION` when IDs/types/gate flags change in a way reports must distinguish.

## Adding an attack

- **LLM kind:** new `attack_method` the simulator graph understands. Coordinate with martian-agents first.
- **Encoding kind:** add a branch in `encoding.apply_encoding` and an `Attack` factory. Keep it side-effect free and reversible when possible (ROT13 is; Base64 encode is not expected to round-trip through the target).

## Public API

`martian_redteam.__all__` is the supported surface. Prefer factories (`PromptLeakage()`) over constructing `Vulnerability` by hand. `ClientError`, `RetryableClientError`, `GateFailure`, and `evaluate_gate` are exported for CI wrappers.

Internal modules (`protocol`, `config`, `catalog.get_*`) may change without a major version while the package is `0.x`.

## Release checklist (internal)

Expected CLI error output follows the documented [Typer stderr](https://typer.tiangolo.com/tutorial/printing/)
and [exit-code](https://typer.tiangolo.com/tutorial/terminating/) conventions.
Keep the diagnostic boundary in `martian._errors` controlled: no raw response
bodies, arbitrary server error codes, or callback exception text in safe reports.
Keep report JSON compatible and test reopened/deep-copied report redaction.

The canonical release procedure is [release preparation](releasing.md). Its
artifact guard checks and clean-installs both wheel and sdist. Strict Mypy
now covers both SDK modules; no eval typing exemption remains.


- [ ] `ci` aggregator green on `main` (lint, types, test 3.12 + 3.13, packaging, action-smoke)
- [ ] Docs match request/response shapes, CLI flags (`validate`, `-c`/`-a`/`-o`), and customer Actions
- [ ] Coverage / JUnit artifacts still produced; Dependabot ecosystems still `uv` + `github-actions`
- [ ] `NOTICE` still accurate if catalog IDs change
- [ ] Publish to PyPI only through the [release procedure](releasing.md), on an explicit release request
- [ ] Do **not** point this package at the generic `martian_agents` assistant as a red-team target
- [ ] Customer examples pin the SDK to a tag or SHA, not floating `@main`

## Related repos

| Repo | Role |
| --- | --- |
| [MartianSecurity/martian-redteam](https://github.com/MartianSecurity/martian-redteam) | This SDK |
| [MartianSecurity/martian-agents](https://github.com/MartianSecurity/martian-agents) | Hosted graphs, Aegra, Bedrock, infra |
| confident-ai/deepteam (local checkout only) | Taxonomy reference, not a publishable fork |
