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
| `Target` | The thing under test: `function` (file and function), `http` (URL, headers, body template, response path, and where its response reports the model that answered: `model_path` in its JSON or `model_header`, one of them, each left out when unset so a target without them keeps its digest), `agent` (a registered agent on the server) or `stored` (outputs come with the cases). The URL and header values are kept as written, with `${NAME}` unexpanded; the values a run reads from the environment are never part of it | none |
| `ScorerVersion` | One way of scoring, pinned to the version that scored: `name`, `kind` (`llm_judge`; `check`, a built-in check; `code`, a team's own Python function, type `python`; `human`), `type`, `version`, judge `model` (an MLflow model URI) and `endpoint` (the base address of the OpenAI-compatible endpoint serving it; left out when unset, so a scorer without one keeps its digest), `parameters`, `criteria`. Never a key | Registered scorer |
| `Policy` | When a scorer's results pass: `direction`, `threshold`, `minimum_coverage` | none |
| `RunSettings` | How a run is carried out, not what it grades: `concurrency`, `batch`, `target_timeout_seconds` (each target call, whatever the target), `judge_max_retries`, `judge_rate_limit_wait_seconds`, `judge_headers` (values as written, with `${NAME}` unexpanded), the local `output` folder, the MLflow location and the run's `limits`. Unset means the runner's default. Unset `limits` are left out when the settings are written, so a suite without them keeps its digest | none |
| `RunLimits` | When a run stops before its last case: `max_seconds` of wall time, or `max_judge_tokens`, the tokens its judges may use, input and output together, as the provider reported them. `None` is no limit, and each start or resume of a run has its limits afresh. `stop(limit, used)` is what a run that reached one records: the limit, its value, what was used and a message | none |
| `Suite` | A dataset (`dataset_name`, `dataset_digest`, and `dataset_location`, where its cases are read from), a target, scorers with policies, the `input_key` judges grade against, its `settings`, and its `repeats` (`Repeats`: `attempts` of each case, the suite's `combine` rule and the rules `checks` name for themselves, by scorer name; left out when it is the default, one attempt, so a suite without repeats keeps its digest). Repeats change verdicts, so they are part of the definition, not a run setting. Versioned: a new version for every change, which records who saved it and when (`created_by`, `created_at`) | none |
| `Run` | One execution over a dataset's cases: `source` (`local`, `console`, `ci`), `state` (`queued`, `running`, `completed`, `partial`, `failed`, `cancelled`, `stopped`), what it ran with, `counts`, `provenance` and timestamps, including `cancel_requested_at` when someone asked to stop it. A run that reached one of its limits is `stopped`: it keeps its finished cases, the others have no result, and it can be resumed. With repeats a case is finished only when all of its attempts ran. Its limits are in `provenance.limits` and the one it reached, `RunLimits.stop`, in `provenance.stopped` and, as a message, in `error`. The model its target reported, when the evals core's worker ran it, is in `provenance.model`: `name`, the latest case's, and `cases`, how many cases each model answered | Run |
| `CaseResult` | What one case produced in one run: `status` (`pending`, `passed`, `failed`, `error`, `skipped`), `inputs`, `expectations`, `output`, `error`, `latency_ms`, `fingerprint`. A case its suite runs more than once has every attempt in `attempts` (`Attempt`: `number`, `status`, `checks` with one `CheckVerdict` per check, `output`, `error`, `latency_ms`, `trace_id`); its own `status` and scores are the attempts combined ([repeated attempts](evals.md#repeated-attempts)). `attempts` is left out when empty | Trace; each attempt's own trace too |
| `Score` | One scorer's verdict on a case result, or on a trace of live traffic (then `run_id` and `case_id` are empty): `value`, `passed` (none when there is no verdict), `rationale`, `error`, `source` (`code`, `llm_judge`, `human`), `source_id`, `trace_id`, `metadata`. A judge score made by the evals core's worker has the model and version that produced it as `source_id` (`model@version`) and in `metadata` (`martian.judge.model`, `martian.judge.model_version`, `martian.judge.endpoint`), with the judge's own version in `martian.judge.version` | Assessment (feedback) |
| `ScoringJob` | Scoring a finished run again with more scorers. Its scores add to the run's and never replace them. A job that applies a saved suite names it (`suite_id`, `suite_version`) and carries its `policies`; `cancel_requested_at` as for a run. A policy judge's guidelines are in its scorer's `criteria`. It runs under its run's limits, and one that reached one is `stopped`, with the limit in `error` | none |
| `Trace`, `Span` | A recorded call to the thing under test, from an eval run or from live traffic, and the steps inside it. A case result is a trace of a run. `session_id` and `user_id` are the application's conversation and user. Search results leave `spans` empty and may give a preview of a long `request` or `response`; the trace's `martian.request_preview_truncated` or `martian.response_preview_truncated` metadata is then `"true"`, and reading the trace gives them whole. A span keeps its attributes as recorded; a store fills a trace's unset model, tokens, session, user and span kinds, inputs and outputs from them through the [trace conventions](evals-core.md#trace-conventions) mapping and names its version in the `martian.trace_mapping` metadata | Trace and spans; session and user are the `mlflow.trace.session` and `mlflow.trace.user` metadata MLflow tracing records, and the in-process binding keeps the model in `mlflow.trace.model` |
| `TraceQuery`, `Page` | Which traces to return (run, session, user, model, ids, text, states, tags, time window, order) and one page of any result with `next_cursor` | none |
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
| `target.timeout_seconds` | `RunSettings.target_timeout_seconds`, for a function or an HTTP target |
| Each entry of `checks`, in order | A `ScorerVersion` named by the check's label. `rubric`, `guidelines` and `conditions` are `kind: llm_judge` with their criteria in `criteria`; `groundedness`, `context_relevance` and `context_sufficiency` are `kind: llm_judge` with no criteria; the other types are `kind: check` with the options they give in `parameters`, under the file's names (`values`, `expectation`, `any_of`, `case_sensitive`, `min_similarity`, `pattern`, `required_keys`, `schema`, `min_length`, `max_length`, `max_ms`, `max_usd`, `max_tokens`, `value`, `path`, `abs_tolerance`, `rel_tolerance`, `parts`, `add`, `subtract`, `total`, `key`, `columns`, `rows`, `kinds`, `allow`, `calls`, `arguments_match`, `order`, `source`, `min_calls`, `max_calls`, `min_turns`, `max_turns`). A [`python` check](evals.md#custom-python-checks) is `kind: code`, type `python`, with `file` (as written, relative to the suite file), `function`, `min_score`, `max_score` and `security` in `parameters`: the model names the code and never holds it, and neither loading nor writing imports the file. A figure (`value`, `abs_tolerance`, `rel_tolerance`, a cell of `rows`) keeps the form it is written in, a number or text such as `"1,234.56"`, so a written file says the same, and a tool-call check's `calls` keep theirs: a tool's name, an object with `name` and `arguments`, and in `tool_trajectory` a list for the calls of one model turn. `suite_file.CHECK_OPTIONS` lists which options each type takes |
| A check's `combine` | `Suite.repeats.checks`, under the check's label. A check that names none has no entry: its rule is decided when the suite runs (`all` for a security check, else the suite's) |
| `repeats`, `combine` | `Suite.repeats.attempts`, `Suite.repeats.combine` |
| `judge.model`, `judge.inference_params` | `model` and `parameters` of every `llm_judge` scorer |
| `judge.max_retries`, `judge.rate_limit_wait_seconds`, `judge.headers` | `RunSettings.judge_max_retries`, `judge_rate_limit_wait_seconds`, `judge_headers` (values as written) |
| `concurrency`, `batch`, `output` | `RunSettings.concurrency`, `batch`, `output` |
| `mlflow.tracking_uri`, `mlflow.experiment` | `RunSettings.mlflow_tracking_uri`, `mlflow_experiment` |
| `limits.max_seconds`, `limits.max_judge_tokens` | `RunSettings.limits` (`RunLimits`) |
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
  of 1 or more, a path with an empty step, or `rows` with two rows of one key, a policy check
  option it cannot use, such as an unknown `kinds` entry, a phrase with no letter or digit, or a
  marker shorter than four letters or digits, or a tool-call check option it cannot use, such as
  a call with no name, a pattern that does not compile, a list of parallel calls in an
  `any_order` trajectory, `source: trace` with no target, or a URL target without
  `source: reply` ([deterministic checks](evals.md#deterministic-checks),
  [tool calls](evals.md#tool-calls-and-trajectories)), or a `python` check without a `file` and a
  `function`, or with a `min_score` above its `max_score`. Whether the function can be called is
  checked when a run starts, since that needs the file imported
  ([custom Python checks](evals.md#custom-python-checks)).

Writing raises `ConfigError` for a suite the file cannot hold: no target (a file without one
grades stored outputs, which is a `stored` target), an `agent` target, `policies` (in a file, a
case passes when every check passes it), `human` scorers, `code` scorers other than a python check
with its `file` and `function`, a pinned scorer `version`,
`llm_judge` scorers with different models or parameters (a file has one judge), run settings
for a target or judge the suite does not have (a time limit for stored outputs, a judge token limit
with no `llm_judge` scorer), and a suite with no `dataset_location`.
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

## Inspect AI logs

`martian evals import inspect` reads the logs [Inspect AI](https://inspect.aisi.org.uk/eval-logs.html)
writes, `.eval` and `.json`, against Inspect's own log schema (`EvalLog`, log format version 2),
and records each log as one run ([imported Inspect logs](evals-core.md#imported-inspect-logs)).
`martian.evals.inspect_import` does the mapping without Inspect installed. An `.eval` log is a zip
of `header.json` and one file per sample; Inspect compresses its entries with Zstandard, which the
`zstandard` package in the `evals` extra reads, and older versions with Deflate.

| Inspect log | Model |
| --- | --- |
| The log file | `Run.provenance["inspect"]`: `log` (its absolute path), `log_sha256`, `format` (`eval` or `json`) and `log_version` |
| `eval.eval_id`, `run_id`, `eval_set_id`, `task`, `task_id`, `task_version`, `task_file`, `model`, `created`, `config.epochs`, `dataset` (without its sample ids), `tags`, `packages` | The same names in `Run.provenance["inspect"]` |
| `status` | `Run.state`: `success` is `completed`, `error` `failed`, `cancelled` `cancelled`. A log still `started` is refused. Also in the provenance |
| `results` | `Run.provenance["inspect"]["results"]`, as Inspect wrote it: each scorer's metrics, scored and unscored samples, and the headline metric. Not-a-number metrics are the text `NaN` |
| `stats.model_usage` | `Run.provenance["inspect"]["model_usage"]` |
| `stats.started_at`, `completed_at`; `eval.created` | `Run.started_at`, `finished_at`; `Run.created_at` |
| `error.message` | `Run.error` |
| `eval.dataset.name` | `Run.dataset_name`, else the task's name. `Run.dataset_digest` is the digest of the samples' ids, inputs and targets, so two runs over the same samples compare whatever their order and epochs |
| `eval.dataset.samples` | `Run.truncated` when the log has fewer samples |
| `eval.scorers` | `Run.scorers`: a `ScorerVersion` per score name, `type` the Inspect scorer and `parameters` its options. `kind` is `llm_judge`, with the grading model as `model`, when the scorer called a model; else `code` |
| A sample, once per epoch | A `CaseResult`, in the dataset's order, each sample's epochs together. Its `case_id` is the sample's `id` for epoch 1 and `<id> (epoch <n>)` for a later one, so runs with different numbers of epochs pair on the first |
| `input` | `CaseResult.inputs.prompt` when it is text; `inputs.messages` when it is chat messages, without their ids (Inspect gives messages new ids every run) |
| `choices` | `CaseResult.inputs.choices` |
| `target` | `CaseResult.expectations.expected_response` |
| `output.completion` | `CaseResult.output` and the trace's response |
| `error.message` | `CaseResult.error`, and the case's status is `error`. The message and traceback are the root span's `inspect.error` |
| `total_time` | `CaseResult.latency_ms` and the trace's latency |
| `started_at`, `completed_at` | The trace's request time; `CaseResult.finished_at` |
| `scores.<name>` | One `Score` per value, below |
| `messages`, `events` | The trace's spans, below |
| `model_usage` | The trace's `tokens`, and `cost` when every model reports one, counting the grader's calls as Inspect does; also the root span's `inspect.model_usage` |
| `id`, `epoch`, `uuid` | Trace tags `inspect.sample_id` and `inspect.epoch`, trace metadata `inspect.sample_uuid` (and `inspect.eval_id`), and root span attributes |
| `metadata`, `limit`, `working_time` | Root span attributes `inspect.metadata`, `inspect.limit`, `inspect.working_time` |

Scores keep Inspect's values exactly, their types too:

| Inspect score | Model |
| --- | --- |
| `value` text, a number or a boolean | One `Score` named after the scorer, with that `value` |
| `value` a dict | One `Score` per key, named `<scorer>/<key>`, as Inspect computes a metric per key; the key is in `metadata["inspect.key"]`. A key's `None` is a score with no value |
| `value` a list | One `Score` per entry, named `<scorer>/<index>`, with `metadata["inspect.index"]` |
| `value` NaN, Inspect's unscored value | A `Score` with no value, `error` `unscored` with Inspect's `reason` (such as `unscored: grader_failed`) and `metadata["inspect.value"]` `NaN`. For a dict-valued scorer, one for each key the scorer gives in other samples |
| `C`, `I`, `P`, `N` and booleans | `Score.passed`: `C` and `true` pass; `I`, `P` (partial credit) and `N` (no answer) and `false` fail. Numbers and other text have no verdict, and the value is kept as it is |
| `explanation` | `Score.rationale` |
| `answer`, `reason`, `metadata` (such as a model grader's `grading` transcript), `history` | `metadata["inspect.answer"]`, `["inspect.reason"]`, `["inspect.metadata"]`, `["inspect.history"]`, with the scorer's name in `["inspect.scorer"]` |
| A scorer that called a model | `source` `llm_judge` and `source_id` the model. Inspect records a scorer's work in a span of type `scorer`; a model call inside it is the grader's. Every other score has source `code` |
| The sample's scores | `CaseResult.status`: `error` when the sample failed, `failed` when a score fails, else `error` when one is unscored, `passed` when one passes, and `skipped` when no score gives a verdict (only numbers or other text, or no scores) |

The trace's spans are the sample's conversation under one root span, named after the task, of
kind `AGENT`, with the sample's input and the completion. Each assistant message is a
`CHAT_MODEL` span whose inputs are the messages since the one before it and whose output is the
message, with its tool calls; each tool call is a `TOOL` span of the call's arguments and the
tool's reply, marked as an error when the tool failed. Messages no reply follows, as in a sample
that failed first, are one more span. Inspect's `model` and `tool` events give the spans their
times, model and token use (`mlflow.chat.tokenUsage`). Text a log keeps once in the sample's
`attachments` is put back in its place.

Inspect's per-epoch reductions (`reductions`), the sample's store, sandbox and the events other
than model, tool and span events are not imported; the log the run names keeps them.

## promptfoo results

`martian evals import promptfoo` reads the JSON file `promptfoo eval -o results.json` writes:
promptfoo's output file around an `EvaluateSummary` of version 3, as its
[configuration reference](https://www.promptfoo.dev/docs/configuration/reference/#evaluation-outputs)
gives it, and records each provider in it as one run
([promptfoo results and configs](evals-core.md#promptfoo-results-and-configs)).
`martian.evals.promptfoo_import` does the mapping without promptfoo installed.

| promptfoo results | Model |
| --- | --- |
| The file | `Run.provenance["promptfoo"]`: `results` (its absolute path), `results_sha256` and `results_version` |
| `evalId`, `results.timestamp`, `results.stats`, `metadata` (such as `promptfooVersion`), `config.description` | `eval_id`, `timestamp`, `stats`, `metadata` and `description` in `Run.provenance["promptfoo"]` |
| `results.prompts` of the provider | `Run.provenance["promptfoo"]["prompts"]`: each prompt's `label`, `raw` and `metrics` |
| A result's `provider` (`id`, `label`) | One `Run` per provider, with `provenance["promptfoo"]["provider"]`, the code `promptfoo-<label or id>-<n>`, and each trace's `model` the provider's id. `Run.target` is `stored` |
| `results.timestamp`, `stats.durationMs` | `Run.created_at` and `started_at`; `finished_at` the timestamp plus the duration |
| `config.description` | `Run.dataset_name`, else the file's name. `Run.dataset_digest` is the digest of the cases' ids, inputs and expectations, so two providers over the same tests compare |
| A result (`testIdx`, `promptIdx`, `repeatIndex`) | A `CaseResult`, in test, prompt and repeat order, with `case_id` `test <testIdx + 1>`, `, prompt <promptIdx + 1>` when the file has several prompts, and ` (repeat <repeatIndex + 1>)` for a later repeat |
| `prompt.raw` (the rendered prompt), `vars` | `CaseResult.inputs.prompt` and `inputs.vars`; the prompt is the trace's request |
| `testCase.assert` | `CaseResult.expectations.assert`; a grading `provider` given as an object is kept as its id only, here and in each score's metadata |
| `response.output` | `CaseResult.output` and the trace's response |
| `success`, `failureReason` | `CaseResult.status`: `failureReason` 2 is `error`, else `passed` when `success`, else `failed` |
| `error`, else `response.error`, when `failureReason` is 2 | `CaseResult.error`, and the trace's state is `error`. A failed assertion's reason, which promptfoo also puts in `error`, stays with its score |
| `latencyMs` | `CaseResult.latency_ms` and the trace's latency |
| `cost`, `tokenUsage.total` | The trace's `cost` and `tokens` |
| `id`, `score`, `namedScores`, `testCase.description` | Trace metadata `promptfoo.result_id`, `promptfoo.score`, `promptfoo.named_scores` and `promptfoo.description`; tags `promptfoo.test_idx`, `promptfoo.prompt_idx` and `promptfoo.provider` |
| `vars`, `response.cached`, the token use and cost | The span's attributes `promptfoo.vars`, `promptfoo.cached`, `promptfoo.token_usage` and `promptfoo.cost` |
| `gradingResult.componentResults[]` (or `gradingResult` itself when it has an `assertion` and no components) | One `Score` each, below |

| promptfoo assertion result | Model |
| --- | --- |
| `assertion.type` | `Score.scorer`; a type seen again in the same row is `<type> (2)`, and a result nested in another's `componentResults` (an `assert-set`) is `<parent>/<type>`. `Run.scorers` has one `ScorerVersion` per name with `type` the assertion type |
| `pass` | `Score.value` and `Score.passed` |
| `score` | `metadata["promptfoo.score"]`; 1 when it passed and 0 when it did not if promptfoo gives none |
| `reason` | `Score.rationale` |
| `assertion`, `namedScores`, `metadata` | `metadata["promptfoo.assertion"]`, `["promptfoo.named_scores"]`, `["promptfoo.metadata"]` |
| `metadata.graderError` | A score with no value and no verdict, and the reason as its `error` |
| A model-graded type or a `promptfoo:redteam:*` grader | `source` `llm_judge`, with `source_id` the assertion's grading `provider` when it names one; every other type has source `code` |

The file's `config` (but its `description`), the results' `traceId` and `evaluationId`, and a
row's `metadata` and `promptId` are not imported; the file the run names keeps them.

## MLflow tracking stores

`martian evals import mlflow` copies an MLflow tracking store, read through MLflow's own SQL
store at the schema revision the pinned `mlflow-skinny` writes, into an organisation
([copied MLflow stores](evals-core.md#copied-mlflow-stores)). `martian.evals.mlflow_import` does
the mapping. Every id is derived from MLflow's, so a second copy of the same store finds what the
first wrote.

| MLflow | Model |
| --- | --- |
| The `Default` experiment (id `0`) | The default project |
| Any other active experiment | A `Project`: `id` a UUID derived from the experiment's id and creation time, `name` the experiment's (120 characters at most), `description` its `mlflow.note.content` tag |
| A deleted experiment, run or overridden assessment | Not copied, and reported |
| A trace | A `Trace` in its experiment's project, under MLflow's trace id (`tr-` and 32 hex digits; another form gets a `tr-` id derived from it). Name, state, time, latency, request and response previews, session (`mlflow.trace.session`), user, token use and cost are MLflow's. Tags and metadata keep everything but MLflow's own `mlflow.*` keys; metadata adds `mlflow_source.trace_id` and `mlflow_source.experiment_id` |
| A trace's spans | `Span`s with MLflow's span ids, parents, types, times, status, inputs, outputs and attributes, read from the tracking store's tables, or, for traces older releases wrote, from the `traces.json` among the trace's artifacts when it is a file on this machine. A trace whose spans cannot be found is copied without them, and reported |
| A person's (`HUMAN`) feedback | A `Score` of source `human`, `source_id` the person, `metadata["martian.annotation"]` `feedback`: an annotation, as the UI records one |
| An expectation | A `Score` with the expected value, no verdict, `metadata["martian.annotation"]` `expectation` |
| A judge's (`LLM_JUDGE`) or code's (`CODE`) feedback | A `Score` of source `llm_judge` or `code`, `source_id` MLflow's (the judge's model or the scorer's name) |
| Feedback value | `Score.value`: a boolean, number or text as it is; a dict or list as its JSON text; a number that is not finite as no value, with an error. `passed` is a boolean's own, `yes` and `no` (MLflow judges' answers) pass and fail; numbers and other text have no verdict. The feedback's error message is `Score.error` |
| `rationale`, `create_time_ms`, `assessment_id`, `span_id`, `metadata`, `overrides` | `Score.rationale` and `created_at`; `metadata["mlflow_source.assessment_id"]`, `["mlflow_source.created_at"]`, `["mlflow_source.span_id"]`, `["mlflow_source.metadata"]`, `["mlflow_source.overrides"]`. The copied score's MLflow assessment is new, so its own creation time is the copy's |
| An evaluation dataset | A `DatasetVersion` in the default project, where the core keeps the organisation's datasets, under the dataset's name (a second dataset of the same name with other content is its next version) |
| A dataset record | A `Case`: `id` the record's id (`dr-...`), `inputs`, `expectations`, `outputs` (an empty one is none), `tags` but MLflow's own, and `metadata["mlflow_source.source"]` the record's source. A record without inputs is not copied, and reported |
| An active run with traces, `FINISHED`, `FAILED` or `KILLED` | A `Run` of source `local` with a `stored` target and state `completed`, `failed` or `cancelled`. `id` MLflow's run id as the UUID it is; `code` `mlflow-` and the run's name; `dataset_name` its first dataset input; times MLflow's. A run still `RUNNING` or `SCHEDULED`, and a run without traces (only parameters and metrics), are not copied, and reported |
| The run's traces (linked to it, or recorded in it: `mlflow.sourceRun`) | One `CaseResult` each, holding the trace. Its `case_id` is the id of the record with the same inputs when the run evaluated an evaluation dataset of the store, else `case-` and 12 hex digits of the inputs' digest, so runs over the same inputs pair; `inputs` and `output` the root span's; `expectations` the trace's expectations; `latency_ms` and `finished_at` the trace's. A trace that is a case of two runs belongs to the earlier one; the later one's case has no trace |
| The case's scores | The trace's judge and code feedback that MLflow records as made in that run (`mlflow.assessment.sourceRunId`), or that names no run. The case's status follows them as for Inspect logs: `error` when the trace failed, `failed` when a score fails, `error` when one has an error, `passed` when one passes, else `skipped` |
| `Run.scorers` | A `ScorerVersion` per score name, `kind` the score's source, `type` `mlflow`, `model` the judge's |
| `Run.dataset_digest` | The digest of the cases' ids, inputs and expectations, so two runs over the same dataset compare |
| `Run.truncated` | Fewer cases than the evaluated dataset's records |
| The run's experiment, id, name, status, user, `mlflow.runType`, metrics, parameters, its own tags and its dataset inputs | `Run.provenance["mlflow"]`, with the store's URL (without a password) and its schema revision |

Registered scorers, labeling schemas, review queues, logged models, registered models and prompts,
run artifacts, and trace and run tags in MLflow's `mlflow.*` namespace have no place in the model
and are not copied; the report counts the first five.

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
suite file is their written form ([above](#the-sdk-suite-file)). Inspect AI logs and promptfoo
results are imported into them ([above](#inspect-ai-logs), [above](#promptfoo-results)), a
promptfoo config is converted into suite files ([converted configs](evals-core.md#converted-configs)),
and MLflow tracking stores copied into them ([above](#mlflow-tracking-stores)). An organisation's [data export](data-export.md) is these
objects' JSON, one per line. The evals core serves them
(MorningHQ/martian-notes#98), and the server imports them when it mounts that core
(MorningHQ/martian-notes#102).
