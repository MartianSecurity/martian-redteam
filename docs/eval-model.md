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
  server uses its row UUID as text. The evals core's API addresses runs, projects, scoring jobs
  and review queues by UUID and traces by MLflow's trace id (`tr-` and 32 hex digits), as the
  server's contract does, so objects it serves take ids in those forms
  ([read routes](evals-core.md#read-routes)).
- Case identifiers come from the data (`id` in a JSONL or CSV row, or its position) and are what
  results, scores and reviews refer to.
- Digests are SHA-256 over canonical JSON (keys sorted, no whitespace, UTF-8), computed by
  `martian.evals.model.digest`. A dataset's digest identifies its content whatever it is called.
  A suite's digest (`suite_digest`) covers every field but `id`, `version`, `digest` and who saved
  the version when (`created_by`, `created_at`), run settings included, so a store that keeps one
  version per digest keeps every change.
- A case result's `fingerprint` is the digest of everything that produced it (see
  [resume](evals.md#run-a-suite-file-locally)), so a resumed run reuses it only while that is unchanged.

## Objects

| Object | What it is | MLflow home |
| --- | --- | --- |
| `Project` | A named body of evaluation work | Experiment |
| `Case` | One test case: `inputs`, `expectations`, optional stored `outputs`, `tags`, `metadata` | Evaluation dataset record |
| `DatasetVersion` | An immutable set of cases: `name`, optional `version`, `digest`, `case_count`, `source` (`file`, `draft`, `global`, `upload`, `traces`) and `location` | Evaluation dataset |
| `DatasetDraft` | An editable working copy of a dataset: `name`, `revision` (raised by every edit), `state` (`editing` or `published`), `case_count`, and the `published_digest` of the version publishing created | none (Martian table) |
| `Target` | The thing under test: `function` (file and function), `http` (URL, headers, body template, response path), `agent` (a registered agent on the server) or `stored` (outputs come with the cases). The URL and header values are kept as written, with `${NAME}` unexpanded; the values a run reads from the environment are never part of it | none |
| `ScorerVersion` | One way of scoring, pinned to the version that scored: `name`, `kind` (`llm_judge`, `check`, `code`, `human`), `type`, `version`, judge `model` (an MLflow model URI) and `endpoint` (the base address of the OpenAI-compatible endpoint serving it; left out when unset, so a scorer without one keeps its digest), `parameters`, `criteria`. Never a key | Registered scorer |
| `Policy` | When a scorer's results pass: `direction`, `threshold`, `minimum_coverage` | none |
| `RunSettings` | How a run is carried out, not what it grades: `concurrency`, `batch`, `target_timeout_seconds`, `judge_max_retries`, `judge_rate_limit_wait_seconds`, `judge_headers` (values as written, with `${NAME}` unexpanded), the local `output` folder and the MLflow location. Unset means the runner's default | none |
| `Suite` | A dataset (`dataset_name`, `dataset_digest`, and `dataset_location`, where its cases are read from), a target, scorers with policies, the `input_key` judges grade against, its `settings`, and its `repeats` (`Repeats`: `attempts` of each case, the suite's `combine` rule and the rules `checks` name for themselves, by scorer name; left out when it is the default, one attempt, so a suite without repeats keeps its digest). Repeats change verdicts, so they are part of the definition, not a run setting. Versioned: a new version for every change, which records who saved it and when (`created_by`, `created_at`) | none |
| `Run` | One execution over a dataset's cases: `source` (`local`, `console`, `ci`), `state` (`queued`, `running`, `completed`, `partial`, `failed`, `cancelled`), what it ran with, `counts`, `provenance` and timestamps, including `cancel_requested_at` when someone asked to stop it | Run |
| `CaseResult` | What one case produced in one run: `status` (`pending`, `passed`, `failed`, `error`, `skipped`), `inputs`, `expectations`, `output`, `error`, `latency_ms`, `fingerprint`. A case its suite runs more than once has every attempt in `attempts` (`Attempt`: `number`, `status`, `checks` with one `CheckVerdict` per check, `output`, `error`, `latency_ms`, `trace_id`); its own `status` and scores are the attempts combined ([repeated attempts](evals.md#repeated-attempts)). `attempts` is left out when empty | Trace; each attempt's own trace too |
| `Score` | One scorer's verdict on a case result, or on a trace of live traffic (then `run_id` and `case_id` are empty): `value`, `passed` (none when there is no verdict), `rationale`, `error`, `source` (`code`, `llm_judge`, `human`), `source_id`, `trace_id`, `metadata`. A judge score made by the evals core's worker has the model and version that produced it as `source_id` (`model@version`) and in `metadata` (`martian.judge.model`, `martian.judge.model_version`, `martian.judge.endpoint`), with the judge's own version in `martian.judge.version` | Assessment (feedback) |
| `ScoringJob` | Scoring a finished run again with more scorers. Its scores add to the run's and never replace them. A job that applies a saved suite names it (`suite_id`, `suite_version`) and carries its `policies`; `cancel_requested_at` as for a run. A policy judge's guidelines are in its scorer's `criteria` | none |
| `Trace`, `Span` | A recorded call to the thing under test, from an eval run or from live traffic, and the steps inside it. A case result is a trace of a run. `session_id` and `user_id` are the application's conversation and user. Search results leave `spans` empty and may give a preview of a long `request` or `response`; the trace's `martian.request_preview_truncated` or `martian.response_preview_truncated` metadata is then `"true"`, and reading the trace gives them whole | Trace and spans; session and user are the `mlflow.trace.session` and `mlflow.trace.user` metadata MLflow tracing records |
| `TraceQuery`, `Page` | Which traces to return (run, session, user, ids, text, states, tags, time window, order) and one page of any result with `next_cursor` | none |
| `Session`, `SessionSearch` | A conversation: the traces that share a session id, each one a turn, with `turns`, `errors`, `started_at`, `last_at`, `preview` and `tokens`. A session search is the sessions among the first `limit` traces (1,000) a query matches, with `has_more`, `traces_examined` and `ungrouped`. Built from traces by `sessions_from_traces`, never stored | none (grouped from traces) |
| `QueueItem` | One trace in a review queue and whether its review is done | Review queue item |
| `ReviewQueue`, `ReviewQuestion` | Case results of one run handed to people, with the questions they answer | Labeling session and review schema |
| `Review` | A person's answer about one case result. A new answer is a new review naming the one it replaces (`previous_id`); reviews are never edited | Assessment (human feedback) |

## The SDK suite file

A [suite file](evals.md#run-a-suite-file-locally) is the written form of a `Suite`.
`martian.evals.suite_file` loads one into the model and writes one back:

```python
from martian.evals.suite_file import cases_to_jsonl, load_suite_file, suite_to_yaml

suite, cases = load_suite_file("evals.yaml")  # the suite, and the cases its data file holds
text = suite_to_yaml(suite)                   # a suite file that loads back to the same suite
data = cases_to_jsonl(cases)                  # a JSONL data file for it
```

`suite_from_yaml(text, name=..., cases=...)` does the same from a file's text. A loaded suite
is named after its file (`evals.yaml` is the suite `evals`), as a local run names its MLflow
experiment. Writing leaves out the suite's id, name, version and dataset digest: the file's name
and its data give them again on the next load.

| Suite file | Model |
| --- | --- |
| `data` | `Suite.dataset_location`, as written. `dataset_name` is the file's stem and `dataset_digest` the digest a store gives its cases as a `DatasetVersion` |
| A row of the data file | `Case` (`id`, `inputs` or `input`, `expectations`, `outputs` or `output`); a JSONL row's `group` is the tag `martian.group` in `Case.tags` |
| No `target` | `Target` of kind `stored`: the outputs come with the cases |
| `target.file`, `target.function` | `Target` of kind `function` |
| `target.url`, `headers`, `body`, `response_path` | `Target` of kind `http`, the URL and header values as written |
| `target.timeout_seconds` | `RunSettings.target_timeout_seconds` |
| Each entry of `checks`, in order | A `ScorerVersion` named by the check's label. `rubric`, `guidelines` and `conditions` are `kind: llm_judge` with their criteria in `criteria`; `groundedness`, `context_relevance` and `context_sufficiency` are `kind: llm_judge` with no criteria; the other types are `kind: check` with the options they give in `parameters`, under the file's names (`values`, `expectation`, `any_of`, `case_sensitive`, `min_similarity`, `pattern`, `required_keys`, `schema`, `min_length`, `max_length`, `max_ms`, `max_usd`, `max_tokens`, `value`, `path`, `abs_tolerance`, `rel_tolerance`, `parts`, `add`, `subtract`, `total`, `key`, `columns`, `rows`, `kinds`, `allow`). A figure (`value`, `abs_tolerance`, `rel_tolerance`, a cell of `rows`) keeps the form it is written in, a number or text such as `"1,234.56"`, so a written file says the same. `suite_file.CHECK_OPTIONS` lists which options each type takes |
| A check's `combine` | `Suite.repeats.checks`, under the check's label. A check that names none has no entry: its rule is decided when the suite runs (`all` for a security check, else the suite's) |
| `repeats`, `combine` | `Suite.repeats.attempts`, `Suite.repeats.combine` |
| `judge.model`, `judge.inference_params` | `model` and `parameters` of every `llm_judge` scorer |
| `judge.max_retries`, `judge.rate_limit_wait_seconds`, `judge.headers` | `RunSettings.judge_max_retries`, `judge_rate_limit_wait_seconds`, `judge_headers` (values as written) |
| `concurrency`, `batch`, `output` | `RunSettings.concurrency`, `batch`, `output` |
| `mlflow.tracking_uri`, `mlflow.experiment` | `RunSettings.mlflow_tracking_uri`, `mlflow_experiment` |
| `input_key` | `Suite.input_key` |

`RunSettings` holds only what the file sets, so an unset setting stays the runner's default and
the written file says no more than the one loaded. `martian.evals.suite_file.SUITE_FILE_FIELDS`
lists every field, and a test fails when the runner accepts a field that has no entry.

Nothing is dropped silently. Loading raises `ConfigError` naming the field for:

- a check option that does not apply to its type, such as `pattern` on `is_json`, and a `judge`
  with no LLM judge check;
- a field of a JSONL case that the runner does not read, or both `input` and `inputs` (or
  `output` and `outputs`) in one case;
- anything `martian evals run` refuses, such as an unknown field, a `json_schema` keyword the
  check cannot apply, a `latency` or `cost` check with no target to measure, or a numeric or
  table option the check cannot read, such as a `value` that is not a number, a `rel_tolerance`
  of 1 or more, a path with an empty step, or `rows` with two rows of one key, or a policy check
  option it cannot use, such as an unknown `kinds` entry, a phrase with no letter or digit, or a
  marker shorter than four letters or digits
  ([deterministic checks](evals.md#deterministic-checks)).

Writing raises `ConfigError` for a suite the file cannot hold: no target (a file without one
grades stored outputs, which is a `stored` target), an `agent` target, `policies` (in a file, a
case passes when every check passes it), `code` or `human` scorers, a pinned scorer `version`,
`llm_judge` scorers with different models or parameters (a file has one judge), run settings
for a target or judge the suite does not have, and a suite with no `dataset_location`.
`cases_to_jsonl` writes the `martian.group` tag as the row's `group` and refuses cases with any
other `tags` or with `metadata`. The written text is then loaded again
and must give the same definition, so a field the writer does not know is refused too.

`martian evals run` loads its suite with `load_suite_file`, so it refuses the same fields before
any case runs, and records the run through the model: a `Run` of the loaded `Suite`, and a
`CaseResult` and `Score`s for each `results.jsonl` record
([command-line runs](evals-core.md#command-line-runs)).

## Local results

Every field of a `results.jsonl` record has a place. `martian.evals.model.RESULTS_RECORD_FIELDS`
lists them, and `from_results_record` converts a record and refuses one with a field it does not
map. `martian evals run` records every case this way:

| `results.jsonl` field | Model |
| --- | --- |
| `id` | `CaseResult.case_id` |
| `passed` | `CaseResult.status` (`true` passed, `false` failed, `null` error) |
| `checks.<label>.passed`, `.rationale`, `.error` | One `Score` per label: `passed` and `value`, `rationale`, `error` |
| `inputs` | `CaseResult.inputs` |
| `output` | `CaseResult.output` |
| `error` | `CaseResult.error` |
| `fingerprint` | `CaseResult.fingerprint` |
| `attempts`, only when the suite repeats its cases | `CaseResult.attempts`: each entry's `number`, `passed` (as `Attempt.status`), `checks` (one `CheckVerdict` per label), `output` and `error` (`martian.evals.model.ATTEMPT_RECORD_FIELDS`) |

`summary.json` is derived: `cases`, `passed`, `failed` and `errors` are `Run.counts`; the per-check
counts and `target_errors` are counted from scores and case results; `pass_rate`,
`confidence_95` and `groups` are computed from those verdicts and the cases' `martian.group` tags
([pass rates and intervals](evals.md#pass-rates-and-confidence-intervals)); with repeats,
`repeats` is the run's `provenance.repeats`, and `attempts` and `reliability` are counted from
the case results' attempts; `false_refusals`, for a suite with a policy check, is counted from
the cases' `no_refusal` verdicts, with repeats their attempts combined
([false refusals](evals.md#false-refusals)); `invalidated`, `seconds_this_run` and `output` describe that one local invocation and
are not stored. A `results.jsonl` record has no `group`: the group belongs to the case, not to
its result.

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

The [evals core](evals-core.md)'s store interface takes and returns these objects, and the
suite file is their written form ([above](#the-sdk-suite-file)). The evals core serves them
(MorningHQ/martian-notes#98), and the server imports them when it mounts that core
(MorningHQ/martian-notes#102).
