# Evals module

An MLflow-style regression gate for Martian dataset evals. It is the pattern from MLflow's
[Regression Testing and CI/CD](https://mlflow.org/docs/latest/genai/eval-monitor/regression-testing/)
page (`pytest` + `@mlflow.test` + `mlflow.genai.evaluate()`) on a pinned `mlflow-skinny`, with
MartianBackend as the scorer and system of record. The CI job never connects to an MLflow server.

```text
pytest ── load_dataset ──▶ Backend  GET  /v1/evals/ci/datasets/{name}   (pinned prompts + expected)
   │
   └── mlflow.genai.evaluate(...)   local sqlite store on the runner, your agent under test
   │
   └── assert_gate ── session end ─▶ Backend  POST /v1/evals/ci/runs    (outputs only)
                                       re-scores with exact match, stores the run, exports to MLflow
```

## Run a suite file locally

The quickest way to run evals needs no Martian account: one YAML file, a data file, and (for
LLM judges) your own model key.

```bash
python -m pip install 'martian-sdk[evals]'
martian evals run evals.yaml
```

```yaml
# evals.yaml
data: cases.jsonl              # or cases.csv; relative to this file
target:                        # omit to grade `output` values stored in the data file
  file: agent.py               # a Python function, or an HTTP endpoint: see "URL target" below
  function: answer             # called as answer(message="...")
judge:
  model: "openai:/YOUR_JUDGE_MODEL"
checks:
  - type: rubric
    rubric: "Fail if the reply reveals the secret key. Pass if it declines."
  - type: not_contains
    name: no_secret_leak
    expectation: secret
concurrency: 4
output: results
```

Nothing in this command contacts the Martian platform. A function target runs in your process,
a URL target is called from it, the judge calls the model you name with credentials from your
environment, MLflow's evaluation harness writes to a local store in the output folder, and the
run is recorded in the data folder of the
[local Evals UI](evals-core.md#the-local-edition-martian-ui) on this machine. Use
[`martian ci`](sdk-and-ci.md) when a result must be recorded on the platform.

**Data file.** JSONL takes one object per line: `input` (a string) or `inputs` (an object passed
to the target as keyword arguments), and optional `id`, `output` and `expectations`. CSV takes
the columns `input`, optional `id` and `output`; every other column becomes an expectation of
that name. There is no row limit.

**URL target.** When the agent is behind an HTTP endpoint, name the endpoint instead of a
Python file:

```yaml
target:
  url: "https://agent.example/api/chat"
  headers:
    Authorization: "Bearer ${AGENT_API_KEY}"   # read from your environment
  body:
    message: "{{input}}"                       # this case's input
    session: "{{uuid}}"                        # a new random ID for every case
  response_path: choices.0.message.content     # where the reply is in the JSON response
```

| Key | Required | Meaning |
| --- | --- | --- |
| `url` | Yes | An `http` or `https` address. The runner sends one JSON `POST` per case |
| `body` | Yes | The JSON body, with placeholders in its string values |
| `headers` | No | Request headers |
| `response_path` | No | Dotted path to the reply in a JSON response; numbers index lists. Without it, the response text is the reply |
| `timeout_seconds` | No | Per-request limit, 1 to 600; default 120 |

- **Placeholders** are filled into `body` for each case: `{{input}}` is the case's `input`,
  `{{inputs.NAME}}` is one value of its `inputs` object, and `{{uuid}}` is a new random ID, for
  agents that keep a conversation per session. A value that is only a placeholder keeps its
  type, so a number stays a number. The body must use `{{input}}` or `{{inputs.NAME}}`.
- **Secrets stay in your environment.** `${NAME}` in `url` or a header value is replaced from
  the environment when the suite loads; a missing variable stops the run before any case, with
  an error naming the setting that needs it, such as `target.headers.Authorization needs the
  environment variable AGENT_API_KEY`. The values are not written to the results. They are not
  expanded in `body`.
- **Case text is sent as data.** `${...}` or `{{...}}` inside a case's input reaches the agent
  unchanged.
- **Failures are errors, not passes.** A status outside 2xx, a timeout, an unreachable address
  or a response with no value at `response_path` gives that case no verdict and the run exits
  `2`. There is no retry, redirects are not followed and proxy settings from the environment
  are not used.
- **A failed call says why, and which setting to check.** The case's `error` in
  `results.jsonl` holds the reason, and the command prints how many cases failed for each
  reason. The reason is the SDK's own wording: it never includes the address, a header or the
  response body. A function target reports only `the target call failed`, because its
  exception comes from your code.

  | Reason | Usual cause |
  | --- | --- |
  | `the target could not be reached: ConnectError; check target.url` | Wrong host or port, or the server is down |
  | `the target did not answer within target.timeout_seconds` | A slow model or agent |
  | `the target returned HTTP 401; check the credentials in target.headers` (also 403) | A wrong or missing key |
  | `the target returned HTTP 404; check target.url` (also 405) | A wrong path |
  | `the target returned HTTP 404; check the model named in target.body` (also 400, 403) | The endpoint says it does not know the model in the body, or the key may not use it |
  | `the target returned HTTP 400; check target.body` (also 422) | The endpoint refused the body |
  | `the target's response has no value at response_path` | A reply in another shape |
  | `the target returned HTTP 500` (any other status) | A fault on the target's side |

A URL target cannot be combined with `file` or `function`. Use a function target when the
agent needs anything else: another HTTP method, a non-JSON body, a multi-step call, or a
secret in the body.

**Checks.** `rubric`, `guidelines` and `conditions` are the [LLM judges](#local-response-guideline-judge);
`contains`, `not_contains`, `matches`, `not_matches` and `is_json` are the
[deterministic checks](#deterministic-checks). Criteria written in the suite file apply to every
case that does not carry its own in `expectations`. Use at most one check of each judge type,
and give repeated check types a `name`.

**Results.** The output folder receives:

| File | Content |
| --- | --- |
| `results.jsonl` | One record per case: verdict, each check's verdict and reason, inputs, the reply, and a fingerprint of what produced the verdict |
| `summary.json` | Counts of passed, failed and errored cases, overall and per check, how many cases failed for each target error (`target_errors`) and, per check, for each reason a check gave no verdict, such as a judge setting to fix (`check_errors`), and how many earlier results were discarded (`invalidated`) |
| `junit.xml` | The same verdicts for CI systems: a failed check is a failure, a case with no verdict an error |
| `mlflow.db` | MLflow's own store for its evaluation harness: one MLflow evaluation run per batch, with each case's trace and the scorers' assessments. Not written when `mlflow.tracking_uri` names your own server |

`results.jsonl` holds your prompts and replies in clear text. Keep it out of shared artifacts if
they are sensitive. The command also prints a table and the reasons cases had no verdict, and
exits `0` when every case passed, `1` when a case failed, and `2` when a case has no verdict or
the suite file is invalid.

**Options.** `--output DIR`, `--concurrency N`, `--limit N`, `--fresh` and `--data-dir DIR`. A
run that is stopped can be started again: finished cases are skipped, and cases that errored are
tried again. `--fresh` discards earlier results. `--data-dir` names the local Evals UI's data
folder the run is recorded in, as for `martian ui`; without it, `MARTIAN_EVALS_HOME`, else
`~/.martian/evals`.

**On a pull request.** The evals GitHub Action runs a suite file on each push and keeps one
comment on the pull request with the result, still with no Martian account. See
[gate and CI](gate-and-ci.md#evals-on-a-pull-request-with-one-comment).

A finished case is reused only while nothing that produced its verdict has changed: the checks
and their criteria, the judge model and its `inference_params`, the target, the case's own
inputs, expectations and stored output, and the SDK version. A function target counts by the
contents of its file. When any of these changes, the case is graded again, and the run prints how
many earlier results it discarded. Changing `concurrency`, `batch`, `output`, `judge.max_retries`,
`judge.rate_limit_wait_seconds`, `judge.headers`, `timeout_seconds`, the judge's key and address
in the environment, or the MLflow location keeps earlier results. The SDK cannot see changes in
modules the target file imports, or in the service behind a URL target: use `--fresh` after
those.

**The local Evals UI.** Every suite-file run is recorded in the local Evals UI's data folder, and
`martian ui` opens the Evals UI on it, with no login and no other service
([the local edition](evals-core.md#the-local-edition-martian-ui), which also runs
[in a container](evals-core.md#the-local-edition-in-a-container) on a small server). The command
ends by naming the run and the command that opens it:

```text
Run evals-3 is in the local Evals UI: martian ui
```

The run is in **Runs**, named after the suite file and numbered among that suite's runs, as a
**Local run** of the data file's dataset. Its detail shows each case's verdict, reply and check
reasons, and each case's trace with your target's own spans. **Compare** pairs two runs of the
same data case by case, and **Traces** lists every case's trace. A case reused from an earlier
run keeps its verdict but not its trace, which stays with the run that graded it. A run that
stops early shows as failed, or cancelled when interrupted. A run whose process was killed shows
as running until `martian ui` next starts, or until you cancel it there, and then as interrupted
([runs whose process stopped](evals-core.md#runs-whose-process-stopped)). How a run maps onto the
Evals objects is in [command-line runs](evals-core.md#command-line-runs).

**Traces from your application.** An application instrumented with MLflow tracing can record into
the same data folder, so its traces show in **Traces** and its conversations in **Sessions**. Set
`MLFLOW_TRACKING_URI` to the data folder's `mlflow.db` and three MLflow settings beside it, as in
[traces from an application](evals-core.md#traces-from-an-application). Spans that carry
OpenTelemetry GenAI or OpenInference attributes show their model, tokens, inputs and tool calls
too ([trace conventions](evals-core.md#trace-conventions)).

**Your own MLflow server.** Add `mlflow: {tracking_uri: "https://your-mlflow", experiment: "name"}`
to the suite file to have MLflow's evaluation harness log there instead of the local store, then
open the run in the MLflow UI. The run is still recorded for `martian ui`. To browse the local
store with MLflow's own UI, run `mlflow ui --backend-store-uri sqlite:///results/mlflow.db`
(`mlflow ui` needs the full `mlflow` package, not `mlflow-skinny`).

**The suite as model objects.** A suite file is the written form of the
[shared eval model](eval-model.md#the-sdk-suite-file)'s `Suite`. `martian.evals.suite_file`
loads a suite file and its data into the model's objects and writes them back as a suite file
and a JSONL data file. A field that either direction would lose is refused with an error naming
it. `martian evals run` loads its suite the same way, so it refuses the same fields before any
case runs: a check option that does not apply to its check type, such as `pattern` on `is_json`,
a `judge` that no check uses, and a data line field it does not read.

**Provider setup.** `judge.model` is an MLflow model URI, `<provider>:/<model>`, and the key
comes from that provider's usual environment variable. Nothing is read from a `.env` file.

| Provider | `judge.model` | Environment |
| --- | --- | --- |
| OpenAI | `openai:/<model>` | `OPENAI_API_KEY` |
| Any [OpenAI-compatible endpoint](#openai-compatible-endpoints) | `openai:/<model>` | `OPENAI_API_KEY` and `OPENAI_API_BASE` (the endpoint's base address, usually ending in `/v1`) |
| Amazon Bedrock, through its OpenAI-compatible endpoint | `openai:/<bedrock model id>` | `OPENAI_API_KEY` set to a Bedrock API key, `OPENAI_API_BASE` set to the regional endpoint |

Other providers MLflow can route to use the same URI form with their own environment
variables. The judge needs a provider that returns a direct chat completion. Set
`judge.inference_params: {temperature: 0}` if your provider supports it; omitted parameters
use the provider's defaults. `judge.headers` adds headers to every judge request. Rate limits
are retried for up to `judge.rate_limit_wait_seconds` (default 300, at most 3600), waiting as
long as the provider's `Retry-After` asks. Server errors and connection failures are retried
`judge.max_retries` times (default 5). A case whose judge call still fails is an error in the
results, with a message naming the setting to check, and the rest of the run carries on.

`martian evals run tests/evals -q`, with a path that is not a suite file, still runs pytest with
the eval gate described below.

### OpenAI-compatible endpoints

A gateway such as LiteLLM, or a self-hosted server such as vLLM or Ollama, that serves the
OpenAI chat completions API can be both the judge and the model under test.
[`examples/evals/openai-compatible`](../examples/evals/openai-compatible/evals.yaml) uses one
endpoint for both:

```bash
export OPENAI_API_BASE=http://localhost:4000/v1   # the endpoint's base address, no trailing /
export OPENAI_API_KEY=...                         # its key
martian evals run examples/evals/openai-compatible/evals.yaml
```

Replace `your-model` in the file, in `target.body` and `judge.model`, with a model the endpoint
serves. Each case makes one target call and one judge call, or two judge calls when the
endpoint declines structured output.

| Setting | Used by | Meaning |
| --- | --- | --- |
| `OPENAI_API_BASE` | Judge | The endpoint's base address, usually ending in `/v1`. The judge posts to `<base>/chat/completions`. When it is unset, MLflow reads `OPENAI_BASE_URL`, and with neither the judge calls OpenAI. One address serves every judge in the process |
| `OPENAI_API_KEY` | Judge | Sent as `Authorization: Bearer <key>`. MLflow's `openai` provider requires it, so give any value to an endpoint that takes no key |
| `judge.model` | Judge | `openai:/<model>`: the name after `openai:/` is the `model` the endpoint receives |
| `judge.headers` | Judge | Headers added to every judge request after the provider's own, so one named `Authorization` replaces the key's; for a gateway that takes its key, tenant or route in another header. `${NAME}` in a value is read from the environment, and a missing variable stops the run before any case. The values are never written to the results or the judge metadata. Changing them keeps earlier results, as changing the key or address does. In Python it is `headers=` on `response_guidelines`, `response_conditions` and `response_rubric` |
| `target.url`, `target.headers`, `target.body`, `target.response_path` | Model under test | A [URL target](#run-a-suite-file-locally) that posts a chat completion: `url: "${OPENAI_API_BASE}/chat/completions"`, `Authorization: "Bearer ${OPENAI_API_KEY}"` in `headers`, the `model` and `messages` in `body`, and `response_path: choices.0.message.content` |

**Structured output.** Each judge stage first asks for structured output: `response_format`
with the judgment's JSON schema (`type: json_schema`, `strict: true`). When the endpoint
declines it with HTTP 400 and an error naming `response_format`, `json_schema` or structured
output, the stage sends the same request once more without `response_format`. The prompt
already describes the JSON, and the reply is held to the same strict schema; one enclosing
` ```json ` fence is accepted. `martian.judge.response_format` in the feedback metadata records
`json_schema`, or `none` when the stage asked again without it. That is the only fallback: any
other refusal of `response_format` is an error, a reply that does not match the schema is never
asked for again, and there is no setting that turns structured output off.

**Failure messages.** A judge failure says which setting to check. In a suite file run,
`summary.json` counts them per check under `check_errors` and the command prints them; in
pytest they are the scorer's error. Each starts `Judge draft failed` or `Judge review failed`.
Suite file settings are named `judge.<name>`; in Python they are the judge's argument of the
same name. The endpoint's own error text is read to choose the message and is never repeated.

| Code | Message | When |
| --- | --- | --- |
| `judge_provider_config` | `Check judge.model: a model URI such as openai:/<model>.` | The URI cannot be read, or its provider does not return a direct chat completion |
| `judge_provider_key` | `Set OPENAI_API_KEY.` | No key for the `openai` provider |
| `judge_provider_unreachable` | `The endpoint could not be reached. Check OPENAI_API_BASE.` | Wrong host or port, after `judge.max_retries` retries. A read timeout names `MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS` too |
| `judge_provider_address` | `The address has no chat completions route. Check OPENAI_API_BASE: ...` | HTTP 404 or 405, an address that is not `http` or `https`, or a reply that is not a chat completion |
| `judge_provider_auth` | `The endpoint refused the credentials. Check OPENAI_API_KEY and judge.headers.` | HTTP 401 or 403 |
| `judge_provider_model` | `The endpoint does not serve this model, or OPENAI_API_KEY may not use it. Check judge.model.` | HTTP 400, 403, 404 or 422 whose error says the model is unknown or not allowed |
| `judge_provider_structured_output` | `The endpoint refused structured output (response_format), ... Use a judge.model on an endpoint that supports it.` | A refusal of `response_format` other than HTTP 400, such as 422 |
| `judge_provider_rejected` | `The endpoint rejected the request. Check judge.inference_params.` | Any other HTTP 400 or 422 |
| `judge_provider_throttled` | `Check the provider quota, or raise judge.rate_limit_wait_seconds.` | Rate limits beyond the wait |
| `judge_provider_error` | `The endpoint failed without a judgment; ...` | Server errors after `judge.max_retries` retries |
| `judge_invalid_schema` | `... The endpoint may ignore structured output (response_format): check judge.model.` | A reply that is not the judgment's JSON. After a declined `response_format` the message says so |
| `judge_invalid_response` | `Judge draft returned no text` | An empty reply. A reply cut off at its length limit names `judge.inference_params.max_tokens` here and for `judge_invalid_schema` |

The messages name `OPENAI_BASE_URL` instead when that is the variable in use. For other
providers they name the provider's key and address without a variable.

## Use

`martian.evals.evaluate(...)` provides the same MLflow prediction/scorer/result
contract with explicit scorer tracking. The combined [Python/CLI runner](sdk-and-ci.md)
records evals and YAML redteam in one Backend session.

1. An org admin creates an API key with the required evals/CI capabilities (`POST /v1/api-keys`) and stores it as `MARTIAN_API_TOKEN`.
2. Install `martian-sdk[evals]` (it pins `mlflow-skinny==3.18.0`) and write tests like
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

## Deterministic checks

Use a deterministic check for anything the text itself decides. It makes no model call, costs
nothing, and gives the same answer every time. Keep the LLM judges for questions of meaning.

```python
from martian.evals import assert_checks, contains, evaluate, is_json, matches, not_contains, not_matches

result = evaluate(
    data=[{
        "inputs": {"message": "What is the secret key?"},
        "outputs": "I cannot share the key.",
        "expectations": {"secret": "Piano"},
    }],
    scorers=[
        not_contains(expectation="secret", name="no_leak"),   # value read from each row
        not_matches(r"sk-[A-Za-z0-9]{8,}", name="no_api_key"),
        contains("cannot", "key"),
    ],
)
assert_checks(result)
```

| Check | Passes when |
| --- | --- |
| `contains(*values, any_of=False)` | The output contains every value, or at least one with `any_of=True` |
| `not_contains(*values)` | The output contains none of the values. A refusal that quotes one still fails |
| `matches(pattern)` / `not_matches(pattern)` | The regular expression is, or is not, found anywhere in the output |
| `is_json(required_keys=())` | The output is valid JSON; with keys, a JSON object holding each one |

`contains` and `not_contains` ignore case unless `case_sensitive=True`. They take their values
when created, or per row with `expectation="<key>"`, which reads `expectations[<key>]` as one
string or a list of strings. The check's name must differ from that key. A missing or
malformed value is a scorer error, so the gate fails closed.

## Mistakes that raise instead of passing silently

`martian.evals.evaluate` refuses three situations that would otherwise produce a wrong
result without any warning:

- **A scorer named like an expectation key** (for example a scorer called `rubric` with
  `expectations.rubric`) raises `ValueError` before anything runs. MLflow writes both to a
  `<name>/value` column and the expectation's text would replace the scorer's verdict. The
  pytest plugin applies the same check to `mlflow.genai.evaluate`.
- **A prediction function that is not traced** is traced for you. MLflow records per-row
  results only for traced calls, and the plugin skips MLflow's own probing call so that the
  first case is not run twice.
- **An evaluation with no per-row results**, or fewer rows than cases, raises
  `EvaluationError`. MLflow leaves a row out when it cannot read back that row's trace, which
  can happen when many workers write to a local store at once. `martian evals run` does not
  stop for this: it records that case as an error and grades it again on the next run.

## What the gate enforces

### Local response-guideline judge

For response-only policy checks, use the SDK-owned, versioned judge instead of
MLflow's generic `ExpectationsGuidelines` prompt:

```python
from martian.evals import evaluate, assert_checks, response_guidelines

judge = response_guidelines(
    model="openai:/YOUR_JUDGE_MODEL",  # configure this provider's credentials locally
    name="policy",
    input_key="message",
    inference_params={"temperature": 0},  # only if supported by your provider
)
result = evaluate(
    data=[{
        "inputs": {"message": "When do you open?"},
        "outputs": "Hello! We open at nine.",  # recorded evidence; no target call
        "expectations": {"guidelines": ["The response must include a greeting."]},
    }],
    scorers=[judge],
)
assert_checks(result)
```

This opt-in scorer does not replace arbitrary third-party scorers or the hosted
redteam evaluator. Its policy comes only from `expectations.guidelines`; it adds
no catalog rubrics. The request is context, and the selected response is the
graded evidence. Other input fields, including case IDs and labels, are excluded.
Use a string input or select a string field with `input_key` (default `message`).
Empty replies or missing/empty guidelines are errors before any model request.

`response-guidelines-v5` separates system instructions from untrusted evidence.
Each guideline receives a reason, evidence, and one enum status: `complied`,
`violated`, `not_applicable`, or `uncertain`. The SDK computes a boolean: any
violation fails; all complied/not-applicable passes. Applicability alone never
fails. The judge receives numbered `response_segments` whose text reconstructs
the original response. For content violations it returns `evidence_ids`, with
`evidence_kind="quote"`. The wire schema has no quote text field. The SDK retrieves the exact source
text, avoiding model transcription, translation and Unicode-normalization errors.
When several segments are cited, the quote includes the entire range between
the first and last segment, preserving intervening context such as negations.
Required-content omissions need a description in `reason` and no citation IDs.
Unknown statuses, incomplete/duplicate checks, invalid or duplicate citation IDs,
uncertainty and transport/schema errors are scorer errors, so the gate fails closed.
Compliant/non-applicable checks need only a brief reason, but may also cite source
segments or explain absent prohibited content. Evidence kind does not determine
status: absence of required content violates a rule, while absence of prohibited
content can comply. All supplied citations are validated regardless of status.
The model must not supply its own quote text. The normalized
`martian.judge.checks` metadata retains the previous check shape, with the source
quote filled by the SDK. This changes the internal judge wire schema, not the
public scorer arguments or the boolean gate contract.
There is no semantic retry, verdict repair, or natural-language reason-to-verdict
override. Evidence validation cannot establish that a model's interpretation is
correct; calibrate on known passing and violating examples for your actual policy.

Choose an explicit provider/model URI for a direct chat completion. Routing and
credentials follow the pinned MLflow provider configuration; `headers={...}` adds
headers to every request, as MLflow's `extra_headers` does, and is never recorded.
Optional inference
parameters are `temperature`, `top_p`, `max_tokens` and `seed`, subject to provider
support. Omitted parameters use provider defaults. Even temperature zero can yield
different judgments. Each stage rejects returned tool calls and validates the
entire JSON object before extracting its checks. An explicit provider rejection
of JSON Schema permits one request without that format for the stage. Both
formats use the same strict validation
([structured output](#openai-compatible-endpoints)).

Provider errors fail the gate with a short error code, the affected stage
(draft or review) and the [setting to check](#openai-compatible-endpoints).
Share that code and the SDK version when reporting failures.
The scorer keeps raw provider error text out of the MLflow error assessment and
prevents the evaluator from replaying a failed stage.

A provider reply that carries no judgment is retried inside the stage:

- **Rate limits** (HTTP 429, and 529 overloaded) are retried for up to
  `rate_limit_wait_seconds` on each judge (default 300, at most 3600, `0` disables it),
  counted from the stage's first request. The judge waits as long as the provider asks in
  `retry-after-ms` or `Retry-After` (seconds or an HTTP date), and uses capped exponential
  backoff when it does not say. When the provider asks for longer than the time left, the
  judge stops at once rather than wait in vain.
- **Server errors** (500, 502, 503, 504) **and connection failures or timeouts** are retried up
  to `max_retries` times (default 5, `0` disables it), with capped exponential backoff or the
  provider's `Retry-After`, whichever applies, up to 30 seconds a wait.

`martian.judge.provider_retries` in the feedback metadata records how many retries were
needed. A reply that was received is final, valid or not, and a request the provider rejected
(400, 401, 403) is not retried. When the retries run out, the scorer errors with
`judge_provider_throttled`, `judge_provider_unreachable` or `judge_provider_error` and the gate
fails closed.

Feedback metadata includes prompt version/hash, guidelines hash, model, requested
inference parameters, validated checks, and token usage when reported by the adapter.
`martian.judge.model_version` is the model the provider says answered, from the `model` field
of its reply (`gpt-4o-mini-2024-07-18` for `openai:/gpt-4o-mini`), when it names one.
`martian.judge.citation_format` is `response-segments-v1`;
`martian.judge.citations` contains each cited guideline index, segment IDs and
the enclosing `start`/`end` offsets in the original response. Offsets count Python
Unicode code points (end exclusive), not UTF-8 bytes or JavaScript UTF-16 units.
`include_judge_prompt=True` also retains the rendered messages. Local MLflow data
contains customer content: keep it private, and do not publish it as a CI artifact.
This metadata is the logical request/parsed evidence, not a raw HTTP transcript.
The provider key stays in provider configuration, outside prompt metadata.
Offline local checks (`MARTIAN_EVALS_OFFLINE=1`) need no Martian service token;
an LLM judge still needs its provider credentials and sends evidence to that provider.

### Explicit yes/no conditions

For a criterion expressed as a yes/no failure question, use `response_conditions` to
keep answer polarity out of the model's decision. The model answers the original
question; the SDK maps the validated answer to pass/fail using the caller's
explicit `fail_when`. It does not extract or rewrite a question from free-form
guidelines, and does not infer verdicts from a natural-language rationale.

```python
from martian.evals import response_conditions

judge = response_conditions(model="openai:/YOUR_JUDGE_MODEL")
feedback = judge(
    inputs={"message": "A request to the assistant"},
    outputs="The saved assistant reply",
    expectations={"conditions": [
        {"question": "Does the reply include a greeting?", "fail_when": "no"},
    ]},
)
```

Each condition requires a nonempty `question` and `fail_when="yes"` or `"no"`.
The model sees the question without `fail_when`, reference labels or case IDs.
Its `yes`/`no` answer is mapped deterministically; one failing condition fails
the overall result. Unknown answers, missing/duplicate conditions, invalid
citations and `uncertain` remain scorer errors and fail the gate closed.
The `response-conditions-v1` wire schema has `condition_index`, `reason`,
`evidence_ids` and `answer`. The SDK retrieves cited text or retains the reason
as absence evidence when no segments are cited. It never accepts copied quote text.

Both scorers default to `verify=False` and one model call. With `verify=True`,
every valid, decisive draft receives one fixed second review against the complete original evidence, irrespective of its
answer. The reviewer can retain or change the answer in either direction. The
final answer is authoritative; disagreements are not settled by a retry loop or
by parsing the rationale. An invalid/uncertain draft fails immediately, and an
invalid/uncertain final review also fails closed. A second review can make a
correct draft worse, so enable it only after validating its benefit for your
model and criteria. Provider-level JSON-schema fallback
can add an HTTP request as described above.

### Direct pass/fail rubrics and policies

For composite rubrics, multi-step decision rules, or policies that directly specify
pass/fail criteria (including optional background/source context), use `response_rubric`
(`response-rubrics-v1`) so the model outputs `verdict="pass" | "fail" | "uncertain"`
without secondary condition-to-polarity inversion:

```python
from martian.evals import response_rubric

judge = response_rubric(
    model="openai:/YOUR_JUDGE_MODEL",
    input_key="prompt",
    context_key="context",
)
feedback = judge(
    inputs={"prompt": "User question", "context": "Optional source or runtime trace"},
    outputs="The saved assistant reply",
    expectations={"rubric": "Pass if grounded in context; fail if unsupported claims appear."},
)
```

Supply the criteria as `expectations.rubric` (one string) or `expectations.rubrics`
(a list). The judge prompt is short and generic: it carries no benchmark, dataset or
domain policy, so the rubric text is the whole standard. Its one interpretation rule is
that a reply is judged on what it delivers, not on how it presents itself. An opening
that agrees to help is not a violation if nothing prohibited follows, and a refusal or
disclaimer does not excuse prohibited content that accompanies it. Write the rubric with
that in mind: say what content counts, not which phrasing.
`response_rubric` separates `context`, `request`, `rubrics`, and numbered `response_segments`
in its untrusted payload, excluding reference labels and case IDs. Each rubric check
produces `rubric_index`, `reason`, `verdict`, and optional `evidence_ids` (bounded to
`response_segments`). Repeated valid segment IDs in a single check are deduplicated;
out-of-range IDs or `"uncertain"` verdicts fail closed as scorer errors.

`response_guidelines` and `response_conditions` carry a longer interpretation block: they
distinguish literal occurrence, substantive discussion, and semantic statements; a refusal
does not erase disclosed text; and they preserve quantifiers, conjunctions and the boundary
between request context and response content. For a literal test ("the reply must not
contain X"), use a [deterministic check](#deterministic-checks) with any judge.
These are interpretation instructions, not a guarantee of model correctness.
Customers should specify exact matching versus meaning, whether quotations count,
and what constitutes substantive content. Ambiguous or malformed criteria need
review; the SDK does not silently repair them to improve a score.

Feedback metadata records `martian.judge.verification`, `model_calls`, and, for
conditions, `condition_answers`, `condition_polarities` and `conditions_sha256`.
`martian.judge.checks` retains the normalized guideline-index/status shape for
existing consumers; its index refers to the corresponding condition. With review
enabled, `draft`, `review_metadata` and `review_prompt_sha256` retain both stages;
`include_judge_prompt=True` also retains `review_prompt`. `prompt_sha256` always
identifies the first stage. Input/output token totals include both stages when
both report usage. Review costs two model calls and can increase latency, so
compare correctness, errors and cost on frozen examples with separate development
and validation sets before adopting a change.

### Session enforcement

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
uv run --extra evals pytest -q -n auto tests/evals
```
