# Shared eval data model

`martian.evals.model` (`martian-eval-model-v1`) is the one set of objects that the SDK, the local
edition and the Martian server use for evaluation work. A suite file, a local `results.jsonl` and
the server's evals tables all describe these objects. This page defines them and maps each
existing format onto them.

The model holds evaluation content only. Tenancy, request receipts, job leases and delivery
progress belong to the edition that stores the objects, not to the model. MLflow stays the store
underneath. An object that lives in MLflow carries its MLflow identifiers in `store`
(`StoreRef`: `experiment_id`, `run_id`, `dataset_id`, `trace_id`, `assessment_id`).

The definitions are pydantic models in `src/martian/evals/model/`. Every object is immutable and
rejects unknown fields.

## Identifiers

- `id` fields are opaque strings, unique within their project. A local run generates them; the
  server uses its row UUID as text.
- Case identifiers come from the data (`id` in a JSONL or CSV row, or its position) and are what
  results, scores and reviews refer to.
- Digests are SHA-256 over canonical JSON (keys sorted, no whitespace, UTF-8), computed by
  `martian.evals.model.digest`. A dataset's digest identifies its content whatever it is called;
  a suite's digest identifies its definition.
- A case result's `fingerprint` is the digest of everything that produced it (see
  [resume](evals.md#run-a-suite-file-locally)), so a resumed run reuses it only while that is unchanged.

## Objects

| Object | What it is | MLflow home |
| --- | --- | --- |
| `Project` | A named body of evaluation work | Experiment |
| `Case` | One test case: `inputs`, `expectations`, optional stored `outputs`, `tags`, `metadata` | Evaluation dataset record |
| `DatasetVersion` | An immutable set of cases: `name`, optional `version`, `digest`, `case_count`, `source` (`file`, `draft`, `global`, `upload`, `traces`) and `location` | Evaluation dataset |
| `DatasetDraft` | An editable working copy of a dataset: `name`, `revision` (raised by every edit), `state` (`editing` or `published`), `case_count`, and the `published_digest` of the version publishing created | none (Martian table) |
| `Target` | The thing under test: `function` (file and function), `http` (URL, body template, response path, header names), `agent` (a registered agent on the server) or `stored` (outputs come with the cases). Header values and credentials are never part of it | none |
| `ScorerVersion` | One way of scoring, pinned to the version that scored: `name`, `kind` (`llm_judge`, `check`, `code`, `human`), `type`, `version`, judge `model`, `parameters`, `criteria` | Registered scorer |
| `Policy` | When a scorer's results pass: `direction`, `threshold`, `minimum_coverage` | none |
| `Suite` | A dataset, a target and scorers with policies, versioned: a new version for every change | none |
| `Run` | One execution over a dataset's cases: `source` (`local`, `console`, `ci`), `state` (`queued`, `running`, `completed`, `partial`, `failed`, `cancelled`), what it ran with, `counts`, `provenance` and timestamps | Run |
| `CaseResult` | What one case produced in one run: `status` (`pending`, `passed`, `failed`, `error`, `skipped`), `inputs`, `expectations`, `output`, `error`, `latency_ms`, `fingerprint` | Trace |
| `Score` | One scorer's verdict on a case result, or on a trace of live traffic (then `run_id` and `case_id` are empty): `value`, `passed` (none when there is no verdict), `rationale`, `error`, `source` (`code`, `llm_judge`, `human`), `trace_id` | Assessment (feedback) |
| `ScoringJob` | Scoring a finished run again with more scorers. Its scores add to the run's and never replace them | none |
| `Trace`, `Span` | A recorded call to the thing under test, from an eval run or from live traffic, and the steps inside it. A case result is a trace of a run. `session_id` and `user_id` are the application's conversation and user. Search results leave `spans` empty | Trace and spans; session and user are the `mlflow.trace.session` and `mlflow.trace.user` metadata MLflow tracing records |
| `TraceQuery`, `Page` | Which traces to return (run, session, user, ids, text, states, tags, time window, order) and one page of any result with `next_cursor` | none |
| `Session`, `SessionSearch` | A conversation: the traces that share a session id, each one a turn, with `turns`, `errors`, `started_at`, `last_at`, `preview` and `tokens`. A session search is the sessions among the first `limit` traces (1,000) a query matches, with `has_more`, `traces_examined` and `ungrouped`. Built from traces by `sessions_from_traces`, never stored | none (grouped from traces) |
| `QueueItem` | One trace in a review queue and whether its review is done | Review queue item |
| `ReviewQueue`, `ReviewQuestion` | Case results of one run handed to people, with the questions they answer | Labeling session and review schema |
| `Review` | A person's answer about one case result. A new answer is a new review naming the one it replaces (`previous_id`); reviews are never edited | Assessment (human feedback) |

## The SDK suite file and results

| SDK | Model |
| --- | --- |
| Suite file `data` | `DatasetVersion` with `source: file`, `location` the path, `digest` over its cases |
| A row of the data file | `Case` (`id`, `inputs` or `input`, `expectations`, `outputs` or `output`) |
| `target.file` and `target.function` | `Target` of kind `function` |
| `target.url`, `body`, `response_path`, `headers` | `Target` of kind `http`; only the header names are kept |
| No target, outputs stored in the data | `Target` of kind `stored` |
| Each entry of `checks` | `ScorerVersion`: a judge type is `kind: llm_judge` with `judge.model` and `judge.inference_params`; a deterministic check is `kind: check` with its options in `parameters` and its rubric, guidelines or conditions in `criteria` |
| `judge.max_retries`, `judge.rate_limit_wait_seconds`, `concurrency`, `batch`, `output`, `mlflow` | How a run is carried out, not what it grades: not in the model |

Every field of a `results.jsonl` record has a place. `martian.evals.model.RESULTS_RECORD_FIELDS`
lists them, and `from_results_record` converts a record and refuses one with a field it does not
map:

| `results.jsonl` field | Model |
| --- | --- |
| `id` | `CaseResult.case_id` |
| `passed` | `CaseResult.status` (`true` passed, `false` failed, `null` error) |
| `checks.<label>.passed`, `.rationale`, `.error` | One `Score` per label: `passed` and `value`, `rationale`, `error` |
| `inputs` | `CaseResult.inputs` |
| `output` | `CaseResult.output` |
| `error` | `CaseResult.error` |
| `fingerprint` | `CaseResult.fingerprint` |

`summary.json` is derived: `cases`, `passed`, `failed` and `errors` are `Run.counts`; the per-check
counts and `target_errors` are counted from scores and case results; `invalidated`,
`seconds_this_run` and `output` describe that one local invocation and are not stored.

## The server's evals tables

| Table | Model |
| --- | --- |
| `eval_projects` | `Project` |
| `eval_runs` | `Run`; `agent_id` is a `Target` of kind `agent` |
| `eval_item_results` | `CaseResult` |
| `eval_assessments` | `Score` |
| `eval_scoring_jobs` | `ScoringJob` |
| `eval_reviews` | `Review` |
| `eval_review_queues` | `ReviewQueue` and `ReviewQuestion` |
| `eval_suite_versions` | `Suite` |
| `eval_dataset_drafts`, `eval_dataset_draft_records` | An editable draft and its `Case` rows; publishing produces a `DatasetVersion` |
| `eval_global_dataset_versions`, `evals_dataset_links` | `DatasetVersion` |
| `eval_judge_operations` | Registering a `ScorerVersion` |

The column-by-column map lives with the tables, in `app/evals_model_map.py` in
MorningHQ/MartianBackend, and a test there fails when an evals table or column has no entry. A
column that is not model content names why: tenancy, request receipts, job custody, delivery to
MLflow, or the dormant Langfuse columns. Tables with no model content at all (API keys, saved
views, store bindings, migrations, delivery progress and CI gate sessions) are listed there too.

## What uses it

The [evals core](evals-core.md)'s store interface takes and returns these objects. The suite
file becomes the written form of these objects (MorningHQ/martian-notes#99), the evals core
serves them (MorningHQ/martian-notes#98), and the server imports them when it mounts that core
(MorningHQ/martian-notes#102).
