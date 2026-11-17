# Security and data

## Trust boundary

```text
Customer process             Approved Backend origin          Agents
local target callback <--- SDK -- Bearer / typed DTOs --------> execution owner
         |                    |                                  |
         +-- target provider  +-- eval outputs / CI records       +-- models
```

Modern SDK clients call only Backend with one scoped platform token. The target
callback remains local and owns its provider authentication. Simulation sends
catalog IDs, purpose, method/mode and an optional prompt. Judging sends attack,
reply, tool identifiers and optional custom criteria. These texts can contain
sensitive target data; they cross the customer boundary and enter the service's
controlled operation/recovery lifecycle. SDK digests do not remove data from
actual model requests. Backend needs raw dataset outputs for canonical scoring.

Requests cannot choose model/service/target endpoints. YAML cannot override the
platform origin. HTTPS is required except loopback; redirects and environment
proxies are off unless proxies are explicitly opted in. Constructor connections
must supply a complete URL/token pair. No `.env` is loaded and unused legacy
variables are ignored; legacy auth is never selected after a failed public call.

A version 2 red team run file that names `models` runs locally instead
(`martian.redteam.engine.LocalService`): the attacker and judge prompts, the
purpose, the attacks and the target's replies go only to the models it names,
with the user's own key, through the judges' model routing. Nothing is sent to a
Martian server, and a run with both `models` and a Martian connection configured
stops rather than choose one.

The local engine treats the target's output as hostile
(`martian.redteam.engine.hostile`). The judge sees the attack, the reply and the
tool calls only as data fields between markers that carry a fresh random id,
after an instruction never to follow anything inside them; a run of three or
more `<` or `>` in that text is broken up, so a reply cannot close its field.
Before any prompt is sent, the value of every environment variable named like
a credential (`KEY`, `TOKEN`, `SECRET`, `PASSWORD`, `CREDENTIAL`, `AUTH`, eight
characters or more) and of the models' headers is replaced by `[redacted]`, so
a key the target leaks never reaches a model provider. The headers themselves
are still sent to the model endpoint. A listener the harness opens binds to
loopback only.

Reports default to content digests. Content opt-in does not disable credential
redaction, including constructor secrets after client close and report copies.
Explicit input/output redaction flags take precedence. Raw in-memory assessment
fields/model dumps and private local MLflow traces are not safe public reports.

## Local suite runs

`martian evals run evals.yaml` does not use the platform connection above and sends nothing to
Martian. Its outbound calls are the ones the suite file names:

- **A URL target** (`target.url`) receives one JSON `POST` per case, or per attempt when the
  suite repeats its cases, carrying that case's inputs and never its expectations
  ([the target and the grader](#the-target-and-the-grader)), and for each combination of the
  suite's [matrix](evals.md#a-matrix-of-prompts-and-models) once more, with that combination's
  values.
  The suite file chooses this address, so review a suite file from someone else before running
  it with your credentials loaded. `http` is accepted as well as `https`; use `https` for any
  endpoint that is not on your own machine or network.
- **Header values** can reference `${NAME}`, read from the environment when the suite loads.
  Only the names appear in the suite file. The values are sent to the target and are not
  written to `results.jsonl`, `summary.json`, `junit.xml`, the MLflow trace or the local Evals
  UI's data folder. They are not
  expanded in the request body, and text inside a case is never expanded. Loading a suite file
  into the [shared eval model](eval-model.md#the-sdk-suite-file), as a store keeps it, keeps the
  URL and header values as written, with `${NAME}` unexpanded, so the environment's values are
  never stored. A literal value is stored as it is: keep credentials in the environment.
- **An LLM judge** receives the case's input, the reply and your criteria at the provider you
  configure.
- **The cheating inspection's judge**, only with `martian evals inspect-cheating --judge MODEL
  --accept-judge-cost`, receives each transcript no signal flagged: the case's inputs, the
  target's steps with what each was asked and returned, and the reply, each cut to 600
  characters, never the expectations ([the judge](evals.md#the-judge)).
- **MLflow** receives inputs, replies and judge reasons: a local store by default, or the
  server named in `mlflow.tracking_uri`.

With the [cache of model calls](#the-cache-of-model-calls) on, a call whose reply the cache
holds is not sent at all.

Every run is also recorded in the local Evals UI's data folder on this machine (`--data-dir`,
`MARTIAN_EVALS_HOME` or `~/.martian/evals`), which `martian ui` reads: each case's inputs, reply,
verdicts and reasons as `results.jsonl` holds them (every attempt's, when the suite repeats
cases), its trace with the target's spans, and the
target as the suite file writes it. Nothing there leaves the machine; treat the folder as you
treat `results.jsonl`.

`verdicts.json`, which every run also writes, holds no input, reply or judge reason: each case's
id, verdicts, the SDK's wording of a target failure, a SHA-256 digest of its inputs and
expectations, and the reasons of the deterministic checks that report counts, sizes or names. It
is what a later run compares with (`--baseline`). With `baseline-branch`, the
[evals action](gate-and-ci.md#compared-with-the-last-result-of-main) keeps the baseline branch's
copy in the repository's Actions cache, which pull requests, including those from forks, can read.

Redirects are not followed and proxy settings from the environment are not used for the target
call. Target errors name the failure, never the address, headers or response body.

`martian evals import promptfoo` records a promptfoo results file in the same folder: each row's
prompt, vars, output and assertion reasons. It does not copy the file's `config` section, which
promptfoo writes with best-effort redaction of provider settings; the run keeps only the config's
`description` and the file's path and hash. An assertion's grading `provider` given as an
object, with settings that may hold a key, is kept as its id only. `martian evals convert
promptfoo` writes suite files and calls nothing: a converted suite calls only the `--target`
function it names, never the config's providers.

`martian evals import mlflow` copies an MLflow tracking store into the same folder. It opens the
store so that it cannot write it (SQLite read-only, Postgres with read-only transactions) and never
migrates it, and reads the trace files the store names on this machine only. A Postgres URL's
password is not printed or stored: the report and each copied run's provenance keep the URL
without it. The folder then holds a copy of the store's traces, inputs, outputs and assessments.
See [copied MLflow stores](evals-core.md#copied-mlflow-stores).

`martian evals export` writes everything the data folder's store and record store hold into the
folder it names: every case's inputs and outputs, traces with their spans, scores, reasons and
reviews, run artifacts, and monitors, schedules and thresholds with their history (alert payloads
and webhook URLs and email recipients among them). It holds no credentials: the local targets and
their keys (`targets.json`, `target-keys.json`) and `settings.json` are not exported, nor is a
threshold's webhook secret, and a target's `${NAME}` references stay unexpanded, as the store
keeps them. Treat an export as you treat the
data folder. See [data export](data-export.md).

### The cache of model calls

The [cache](evals.md#a-cache-of-model-calls) is off unless the suite file has a `cache` block or
the run is given `--cache`. With it on, `martian evals run` stores model replies on disk:

- **Where.** The suite file's `cache.folder`, else `cache` in the local Evals UI's data folder
  (`--data-dir`, `MARTIAN_EVALS_HOME` or `~/.martian/evals`). The folder and its subfolders are
  created readable by their owner only (`0700`) and each entry likewise (`0600`). Nothing in it
  leaves the machine, and nothing outside the run reads it. A function target's process cannot
  reach it ([the target and the grader](#the-target-and-the-grader)): it holds judgments.
- **What an entry holds.** One JSON file per reply, named by the SHA-256 digest of its key: the
  format, the kind (`target` or `judge`), when it was stored, and the reply. A target's reply is
  the text or value at `response_path` the endpoint returned, or a function target's return
  value. A judge's is its judgment, the per-criterion verdicts with their reasons, and the
  reply's metadata, such as the response format and the model version the provider named. An
  entry holds no request, no header and no key, so the case's input appears in it only where the
  reply quotes it. Treat the folder as you treat `results.jsonl`: replies can hold whatever the
  target said.
- **What the key leaves out.** The key is a digest of the full request, which is never written
  down. Credentials are not part of it: a header or a query parameter whose name says it holds
  one (`Authorization`, `Proxy-Authorization`, `Cookie`, and any name that holds `auth`, `key`,
  `token`, `secret`, `pass`, `credential`, `cookie`, `session` or `signature`, or is `sig`,
  `code` or `pwd`) is left out, and so is a user name or password in an address; a judge's key is
  never read into it. Other header values are part of it as sent, with `${NAME}` read from the
  environment, so a secret kept in a header whose name says nothing of it, such as
  `X-Customer: ${CUSTOMER_SECRET}`, enters the digest: name such headers for what they hold.
- **Errors are never stored**, so a cached entry is always a reply the model gave.
- **Replayed results are marked.** A run that used the cache says so in `summary.json`, the
  printed summary, the pull-request comment and the recorded run's `provenance.cache`, with how
  many replies came from the cache.
- **Sharing a folder** shares the replies in it. Point `cache.folder` at a shared location, or
  keep the folder in a CI cache, only where everyone who can read it may read the replies, and
  remember that a pull request from a fork can read the repository's Actions cache. An entry
  older than `cache.ttl_seconds` is not used, but stays on disk until a successful call replaces
  it or the folder is deleted.

`martian evals push` is the one command that sends the data folder's content off the machine, and
only when it is run: each finished run's case inputs and outputs, scores, reasons, scoring jobs and
traces with their spans go to the server `MARTIAN_API_URL` names, signed in with the API key in
`MARTIAN_API_TOKEN` over HTTPS (HTTP only to this machine), as the platform connection above.
Reviews, artifacts, datasets, local targets and their keys, and `settings.json` are not sent; a
run's target goes as the run keeps it, with `${NAME}` references unexpanded. The command never
prints the server's answers, only their status. See [pushed runs](evals-core.md#pushed-runs).

## The target and the grader

An agent under test that can reach its grader can read its answers or change its results. So the
SDK sends a target each case's inputs and nothing else, and keeps what grades it, the expectations,
the checks and their criteria, custom scorers' code, the results and the stores, where the target
cannot reach them. How far that goes depends on the kind of target:

| Target | Given | Runs | Cannot reach | Can still reach |
| --- | --- | --- | --- | --- |
| A suite file's function target (`martian evals run`) | The case's inputs, as keyword arguments, and the values of the combination of the suite's [matrix](evals.md#a-matrix-of-prompts-and-models) it runs; in a conversation's turn also its history and session id, as far as its parameters take them | In a process of its own, started with the run (`python -P`), which every combination of the matrix calls and which never imports the evals package | The run's memory, where the expectations, checks and scorers are. Its environment has no `MARTIAN_*` variables and none of MLflow's tracking, experiment, registry, run or workspace variables. On Linux with Landlock, the suite file, the data file, each `python` check's file and the `__pycache__` folder beside it, the output folder, a local SQLite or file `mlflow.tracking_uri`, the local Evals UI's data folder and the suite file's own `cache.folder` when the run uses the cache: it cannot read, change, create or remove them, nor create or remove anything directly in the folders that hold them, nor read the memory of the command's process | Everything else its user can: other files, such as a module a scorer imports or another copy of the answers, the names in the hidden folders, the network, and the environment's other variables, such as a judge's model key. Without Landlock (macOS, Windows, an older or unconfigured Linux kernel) the files above too; the command says so and `summary.json` records it as `target_isolation` |
| A suite file's URL target | One JSON `POST` per case, its body filled from the case's inputs and `{{uuid}}`; a placeholder that names anything else is refused when the suite loads | Wherever the endpoint runs | Nothing of the run's process: it receives the request and nothing else | Whatever its own host lets it. An endpoint on this machine that runs as your user can read the suite's files |
| A function in the pytest gate (`evaluate`, `predict_fn`) | The case's inputs, as keyword arguments | In the test's own process | Nothing is hidden from it | The test's memory, which holds the dataset's expected values, and its files |
| A function target in the evals core's worker (`Target(kind="function")`) | The case's inputs, as keyword arguments | In a process of its own for each run, started before its first case | The work's process, which holds the stores and every case's expectations. On Linux with Landlock, the worker's private paths (`WorkerServices.private_paths`), which the setup names: its stores' files | As a suite file's function target. A store on a server, such as Postgres, is reachable over the network by anything that has its address and credentials, which the target's process is not given unless they are in a variable it keeps |
| A function in the worker's own process (`TargetCall`) | A `Case` with the case's id and inputs only | In the work's process | Nothing is hidden from it: for tests and for code the installation trusts with its stores | The stores, the work's memory and its files |
| An HTTP target of the worker | As a suite file's URL target; a placeholder that names anything else is refused when the worker checks the target | Wherever the endpoint runs | As a suite file's URL target | As a suite file's URL target |
| A registered agent | A `Case` with the case's id and inputs only, to the `AgentExecutor` | Through martian-agents | The worker's process and stores | What its own sandbox lets it |

Scorers run after the target replies, in the process that grades: the command's process for a suite
file, and the work's process in the worker. A judge is given the input, the reply and its
criteria, and never calls the target.

An edition's [case hooks](evals-core.md#case-hooks-and-installed-checks-in-the-worker) run in the
work's process too, and are given each case without its expectations or stored output, so they
cannot hand the target its answers. What a hook adds to the target's inputs
(`CaseState.target_inputs`) is the edition's to keep free of them; its other state reaches only the
installed checks and the teardown, and neither is recorded.

`tests/evals/test_grader_isolation.py` proves the function targets' part. Its target looks for the
answers everywhere it can: in its arguments, in the suite file, the data file, the scorer's file,
the results folder, MLflow's store and the local Evals UI's data folder, in its environment, in its
own memory and in the memory of the process that started it. Each answer is a random value placed
in the data file, the suite file and the scorer, and the test checks that the files and stores hold
them. The target finds none, in a suite file run and in the worker, though it finds a control value
in a file beside it and another in its environment, and it can neither change the results and the
stores nor plant a file beside them.

What isolation leaves reachable, and what an agent tries anyway, shows in its transcript.
[`martian evals inspect-cheating`](evals.md#inspect-a-run-for-cheating), and the evals core's
[route for a run](evals-core.md#runs-inspected-for-cheating), read each case's trace and reply for
signs that the agent reached for its answers, tests or grader: a step that names the run's hidden
files, an answer key, results or a grader's file, the evals or MLflow API, the run's environment
variables or another process, a refusal by isolation, an expected answer it had before any step
produced it, or a reply that writes to the grader. Isolation records nothing itself, so a refused
attempt shows only where the target's own step records its error. Its miss and false-alarm rates on
a labelled set are in [how well it catches cheating](evals.md#how-well-it-catches-cheating).

## Custom Python scorers

A [custom Python check](evals.md#custom-python-checks) is code, and it is **not sandboxed**. It
runs with the full privileges of the process that calls it: it can read that process's
environment (model keys, a `MARTIAN_API_TOKEN`), any file its user can read, and the network, and
it is given each case's inputs, reply and expectations. Nothing in the SDK limits what it imports,
how long it runs or how much memory it takes.

**Who can add one, and where it runs.**

| Where | Who adds it | Where the code runs |
| --- | --- | --- |
| A suite file (`type: python`) | Whoever writes the suite file and its scorer file. Review both before you run a suite from someone else, as you would a function target | In the `martian evals run` process, imported as the run starts, where the run's function target cannot reach it ([above](#the-target-and-the-grader)), and in the `martian evals audit` process, which runs it on the [audit's replies](evals.md#audit-a-suite). Loading, validating or writing a suite file (`martian.evals.suite_file`) only checks the `file`, `function` and bounds as written, and never imports the file |
| Python (`python_check`) | The test's author | In the test's process |
| The [evals core's worker](evals-core.md#custom-scorers-in-the-worker) | Whoever deploys the worker: its setup imports the function and installs it under a name (`WorkerServices.scorers`), and an edition's case hooks and installed checks the same way (`WorkerServices.hooks`, `WorkerServices.checks`) | Only in each piece of work's own process: a custom scorer on a scoring job's saved outputs, an installed check on a run's answered cases and a scoring job's saved outputs. A [monitor](evals-core.md#monitors) runs none, so neither sees a project's production traces: the `Monitor` record refuses a scorer of kind `code`, which both are |
| The local edition (`martian ui`) | Nobody: it installs none | — |

**Never in the API process.** No route of the evals core takes code, a file or a module to import,
and no record names one: a scoring job's `code` scorer is matched by name to what the worker's
setup installed. The process that serves the API never imports or calls a custom scorer on any
path that could reach one:

- **Suite loading and validation** check the shape only, as above.
- **The scoring dialog's routes**: `GET /v1/evals/scorers` offers the built-in scorers alone, and a
  scoring request or a saved suite (`POST /{run_id}/scores`, `POST /suites`) names built-in
  scorers and the [deterministic checks](evals-core.md#deterministic-checks-in-the-worker) of a
  suite file only, as the frozen contract does; any other name, and a `python` or `installed` check, is refused
  with `422`. A check's regular expression runs in the worker's process, as a suite file's does on
  the machine that runs it, and is not time-limited; a worker that serves other people's requests
  should treat a pattern as such.
- **Judges** are handed only judge scorers: a request to the SDK or Agents judge never carries a
  custom scorer.
- **The worker's setup** is imported by module in the API process, to check that a new process can
  import it, so the setup imports each scorer's module inside its function, never at the top of
  its own module.

`tests/evals/test_custom_scorers.py` serves the core with a `ProcessWorker` whose setup installs
scorers from a file that records the process id of every process importing it, calls those
routes, scores a run with the scorers, and fails if the test's own process, which serves the API,
ever imported the file.

**What the worker isolates, and what it does not.** `ProcessWorker` runs each piece of work in a
new process for one organisation's work, so a custom scorer's code never runs in the API process,
MLflow's process-wide state stays with that organisation, and the scorer is handed one case at a
time: that case's inputs, saved output and expectations. It does not isolate the code from
anything else the worker can reach. The work's process runs as the same operating-system user as
the API process, with all of its environment, so any key or database address the API process
holds; beside the setup's store and record store, whose databases hold every organisation's
records; and with the same files and network. There is no time or memory limit: a scorer that
never returns holds its scoring job, and one that ends its process fails the work ("The worker
process ended before this work finished"). Installing a custom scorer on a server is deploying
code into the worker, with the worker's privileges; review it as such. The same holds for an
edition's case hooks and installed checks: they are the edition's code in each work's process, with
no time limit, and a suite or a record names an installed check by name only.

## Telemetry

The evals and red team processes send no telemetry, and neither do the libraries they bundle: `martian ui`,
`martian evals run`, `martian redteam`, the eval gate, a server that serves the [evals core](evals-core.md) and
each of its worker processes. Importing `martian.evals` sets `MLFLOW_DISABLE_TELEMETRY=true`
before it imports MLflow, unless the environment already sets the variable, and each worker
process inherits it. MLflow decides when it is first imported whether to start its telemetry
client, which otherwise asks `config.mlflow-telemetry.io` for a configuration and then posts
usage events from a background thread. A process that imports MLflow before `martian.evals`,
such as an application that serves the core beside its own MLflow code, must set the variable
in its environment.

| Setting | In the evals processes | Effect |
| --- | --- | --- |
| `MLFLOW_DISABLE_TELEMETRY` | `true` unless the environment sets it | MLflow's usage telemetry. `false` turns it back on |
| `DO_NOT_TRACK` | Not set | `true` also turns MLflow's telemetry off |

What each part sends without a setting of yours:

| Part | Sends by itself | Sends only when you configure it |
| --- | --- | --- |
| `mlflow-skinny` | Usage telemetry, off as above. A model price catalogue from github.com, to price a traced model call and never for a judged call ([below](#judges-and-the-model-catalogue)) | Runs and traces to the MLflow server that `mlflow.tracking_uri` in a suite file or an application's `MLFLOW_TRACKING_URI` names; a Databricks workspace for a `databricks` tracking URI. The local edition's store opens its own SQLite files and ignores the tracking URI |
| `opentelemetry-sdk`, under MLflow tracing | Nothing | Spans to the collector `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` or `OTEL_EXPORTER_OTLP_ENDPOINT` names |
| `databricks-sdk` and `google-auth`, which MLflow brings | Nothing | Databricks and Google Cloud URIs, which the local and on-premises stores do not use |
| FastAPI, Starlette, Uvicorn, Pydantic, SQLAlchemy, Alembic, HTTPX, Requests, Typer, Rich | Nothing; none has telemetry | — |
| The Evals UI | Nothing. Its scripts, styles and Inter font files are in the wheel, `martian ui` serves them, and the UI calls only `/v1` on its own origin. It has no analytics. Its sidebar links to the documentation and to this repository; a link opens in a new tab only when you click it | — |
| Judges and targets | Nothing | The model provider a judge names and the address of a URL target ([above](#local-suite-runs)). A judged call connects to its endpoint and to nothing else ([below](#judges-and-the-model-catalogue)) |
| Threshold alerts | Nothing | A threshold's webhook URL and the SMTP server, once the worker's alert settings name them ([below](#threshold-alerts)) |

`tests/evals/test_no_outbound.py` proves it. It runs a local run of a function target with a
deterministic check, `martian ui` serving the UI and that run, and the core with a process
worker running a function target, each in a clean environment (no CI or pytest variables, under
which MLflow turns its telemetry off by itself, and no telemetry settings) with every connection
and name lookup for an address other than loopback refused. It fails on any such attempt and on
MLflow's telemetry client starting in any of those processes, and checks that the shipped UI
loads nothing from another host. It also grades one request with the SDK judge at a loopback
endpoint, with and without an address policy, with every other address refused, and fails if the
judge looks up or connects to anything but that endpoint.

## What a local red team run connects to

A local red team run (`martian redteam run` with `models` in its run file and no Martian connection)
connects to the model it names and to its target, and to nothing else. It sends no telemetry, checks
for no update and needs no account. Every connection it can open:

| Connection | When | What is sent |
| --- | --- | --- |
| The attacker and judge model's endpoint | Always: `models.attacker` and `models.judge`, through the judges' model routing ([evals](evals.md)). For an `openai:/` model, `OPENAI_API_BASE`, else `OPENAI_BASE_URL`, else OpenAI | The catalogue's prompts, the purpose, the attacks, and the target's replies and tool calls, with credentials redacted ([trust boundary](#trust-boundary)); the model's key and `headers` |
| The target | Always: a `url` target, an `mcp` target's `url`, or none for a `file` function or an `mcp` `command`, which run on this machine | The attacks |
| The recording proxy | When the run file lists `tools.proxy`: it listens on loopback, and connects to each `upstream` | The agent's tool calls, as the agent sent them |
| The agent's session API | When the run file names `tools.session_api`: one request to `{session_api}/sessions` | The proxy's session |
| A model price catalogue | Only when `MLFLOW_MODEL_CATALOG_URI` names one, to price `budget.cost_usd` ([below](#judges-and-the-model-catalogue)) | Nothing of the run |

A run with no `models` uses the Martian platform instead, with `MARTIAN_API_URL` and `MARTIAN_API_TOKEN`,
and says so; a run never chooses one when both are configured ([run file models](redteam-run-file.md#models)).

The other red team commands connect to nothing: `validate`, `catalog`, `report`, `compare`,
`pin add`, `convert promptfoo`, `import promptfoo` and `import garak`. `pin run` connects only to
the target, and the proxy's upstreams when the run file lists them. `martian ui` listens on loopback
and serves the runs from the local store. `martian redteam garak` runs garak, a separate program
installed by you; what garak itself connects to is garak's ([red team interop](redteam-interop.md#garak-probes)).

`tests/redteam/test_no_outbound.py` proves it in CI. It runs the local workflow, each command in its
own process with only a model key and the model's address in a clean environment (no Martian, CI,
pytest or telemetry variable): validate, a run writing every report format, a second run, reports
written again, the two runs compared, a confirmed finding pinned and re-run, a promptfoo config
converted and its results imported, and `martian ui` serving the runs. Every connection and name
lookup for an address other than loopback is refused and fails the test, as MLflow's telemetry
client starting does, and the loopback connections must be exactly the model and the target.

## Judges and the model catalogue

A judged call connects to its endpoint and to nothing else. Two things stand in the way of that, and
the core handles both.

**The model catalogue.** MLflow prices the tokens a model call reports from a catalogue of models and
their prices that it downloads from github.com
(`https://github.com/mlflow/mlflow/releases/download/model-catalog%2Flatest`, one `<provider>.json`
file for each provider). It makes the download at the first priced call of a process and tries it
four times, with a five-second timeout each, so an installation with no route to github.com waits on
it before its first verdict, and one that audits its outbound traffic sees the attempts. A judge
asks for a price only when `MLFLOW_MODEL_CATALOG_URI` names a catalogue (`martian.evals._judge_transport`),
which MLflow reads: a mirror (`https://`) or a folder (`file://`) with the catalogue's
`<provider>.json` files under it. With the variable unset or empty, the default, a judged call makes
no download and its metadata leaves out `mlflow.assessment.judgeCost`, the price of the call.
Nothing in the core reads that price, and the tokens, the model version and every verdict are
recorded as before.

The same variable also decides where MLflow prices the model calls of a traced target (the
`cost` check's `max_usd`, [checks](evals.md#deterministic-checks)): it is not a judged call, so it is
not changed, and an installation that traces a target's model calls and has no route to github.com
sets `MLFLOW_MODEL_CATALOG_URI` to a mirror or to an empty address in that process's environment.

**The endpoint's address.** A name can answer one address when it is checked and another when the
HTTP client connects (DNS rebinding), so checking an endpoint's address before a call does not stop
a call to an internal one. An edition that calls endpoints other people name passes an address
policy to `sdk_judge(address_policy=...)`
([the worker's judge](evals-core.md#the-workers-judge)): the judge then connects to the address
its policy allowed, which it resolved when the connection opened, and to no other. The core sets no
policy by itself: a local run calls the endpoint its user named, which may be on the same machine.
The core's other outbound calls are not pinned: a URL target (`martian evals run` and the worker's
HTTP targets) and a webhook of a [threshold alert](#threshold-alerts), whose address check resolves
the name once and the HTTP client again.

`tests/evals/test_no_outbound.py` (a judged call reaches only its endpoint),
`tests/evals/test_worker_judges.py` (the price) and `tests/evals/test_judge_address_pin.py` (the
address) prove both.

## Threshold alerts

The alerts of [thresholds](evals-core.md#thresholds-and-alerts) on monitor scores are the evals
core's own outbound calls. An installation makes none until its operator configures them: the
worker's `AlertSettings` (`martian.evals.alerts`) send no webhook and no email by default, the
routes refuse a threshold with a channel the worker does not send, and so an on-premises install
sends nothing for alerts unless it is set up to (`test_no_outbound.py` runs without alert
settings and still sees no outbound attempt).

What leaves once a channel is configured:

- **A webhook** is one `POST` per alert, and per retry, to the URL a threshold names, which any
  member with the `score` capability chooses. It carries the threshold's definition and name, the
  monitor's id, the scorer, and the window's count, estimate and interval: no trace, prompt,
  response, rationale or user. The URL is `https`, or `http` only to `localhost`; redirects are not
  followed and proxy settings from the environment are not used. Before each attempt the host is
  resolved, and a loopback, private, link-local or reserved address is refused unless the
  installation sets `WebhookSettings(private_addresses=True)`, so on an installation that does not,
  a member cannot aim alerts at the worker's own network. A name that resolves to another address
  between that check and the connection is not caught; where that matters, run the worker where it
  cannot reach internal services.
- **An email** carries the same content as plain text to the threshold's recipients, through the
  SMTP server the operator names, with STARTTLS by default.

Secrets:

- **The webhook secret** is given once, by the member who creates the threshold, and signs each
  request (HMAC-SHA256 over the timestamp and the body; the receiver's check is in
  [sending alerts](evals-core.md#sending-alerts)). The record store keeps it in a column of its own
  (`martian_thresholds.secret`), not in the threshold's record, and only `threshold_secret`, which
  the worker calls to sign, reads it back. No route returns it (a threshold shows
  `"signed": true`), an alert's stored record and payload do not hold it, no log line holds it, and
  `martian evals export` exports the threshold without it: a threshold imported into a fresh
  install shows `"signed": false` and sends no signed webhook until a member gives it a secret
  again (`POST /v1/evals/thresholds/{id}/secret`, once, for a webhook with none). It is stored as
  given, not encrypted: whoever can read the record-store database can read it, as they can every
  record there, and on Postgres row-level security keeps it to its organisation. To change it,
  delete the threshold and create another.
- **The SMTP password** is part of the worker's settings, given by the operator in code from their
  own secret store. No record holds it, so no export does, and the settings' repr and the log
  lines leave it out.
- **Logs and errors.** A failed attempt is logged, and kept as the alert's `last_error`, as a status
  or an error class (`HTTP 503`, `ConnectTimeout`, `SMTP 550`): never the URL, a header, the body,
  the secret, the password or a recipient. The HTTP client's own request log (HTTPX at `INFO`),
  if an installation turns it on, names the URL, so a webhook URL holds no credential: the
  signature authenticates an alert, and a URL with a user name or password is refused.

`tests/evals/test_thresholds.py` checks that the default settings send nothing and open no
connection, that the loopback receiver is refused without `private_addresses`, that no route
answer, log line or alert holds the secret, and that failures are logged without the address.
`tests/evals/test_data_export.py` checks that no file of an export holds a threshold's secret.

## The local edition has no login

`martian ui` serves the evals core and the Evals UI for one fixed user with no sign-in. Anything
that reaches its port can read and change all of its data, and send it traces over OpenTelemetry
(`POST /v1/traces`) without credentials. On a laptop it listens on 127.0.0.1
and answers only the `Host` names `127.0.0.1` and `localhost`, so another machine cannot reach it
and a web page cannot reach it through a name that resolves to the laptop. The container image
listens on the container's interfaces, keeps the same `Host` check and prints a warning when it
starts. Publish its port on 127.0.0.1 only and reach it from elsewhere through an SSH tunnel.
Never expose it publicly. See [the local edition in a container](evals-core.md#the-local-edition-in-a-container).

The targets a user defines in its UI keep their keys in `target-keys.json` in the data folder, a
file with mode `0600` that only the user can read. Only the process that runs a run reads a key,
to send it to that run's target in the target's key header; the server's own process never does.
No answer of the server, no log line and no other file in the folder holds a key, traces
included; a run records the key only as the reference `${MARTIAN_TARGET_KEY}`. Anyone who can reach the port cannot read a key back through it, and
cannot point an existing target, and so its key, at another address: a target cannot be edited.
They can still remove targets and define new ones. See [local targets](evals-core.md#local-targets).

## What a run response shows

The record store keeps a run's whole provenance for the server; the evals core's routes show
only what a run's readers may see, through one allowlist of keys
([run provenance](evals-core.md#run-provenance)). Every route that answers with a run or with
its provenance, in the hosted server's core and in the local edition alike, leaves out:

- the CI token that uploaded a CI run, its id and its name (`token_id`, `token_name`), and the
  digests of the upload request and of its items (`request_digest`, `items_digest`), which the
  server's CI gate records in the run's `provenance.ci`. A CI run's attribution is shown as its
  `ci` with the public keys only: commit, ref, repository, workflow run URL, kit version,
  threshold and minimum coverage;
- anything a worker pins with a target beyond its kind, id, name and digest, such as a registered
  agent's connection and approval;
- any key the allowlist does not name, whoever writes it.

Other things the routes show whole are what the server shows as well: a score's metadata (an
assessment's `provenance`: the judge's model, versions, digests of what it read and its usage,
never a key, which a judge request does not carry) and a trace's tags and metadata, which the
traced application or the worker records. A target's URL and headers, a suite's run settings and
a judge model's endpoint are not in a run response.

## What store errors contain

An error of the evals store, the record store or the work queue never quotes what was written.
When any of them refuses a request, its `StoreError` names the operation, the store method such as
`record_trace`, and what was refused. For a write the database refused, that is the table, the
column or constraint when Postgres names one, and the driver's error class and code:
`record_trace: the database refused a value for trace_request_metadata (StringDataRightTruncation, SQLSTATE 22001)`.
It holds no value written: no request, response, case, score, review, id or name. The same text is
the `detail` of the API's `404`, `409` or `422`, the `error` of a failed run, and the worker's log
line.

- The database's and MLflow's own messages are not passed on, because both can quote values:
  SQLAlchemy writes a statement's values into its errors, and Postgres repeats a duplicate key's.
  The one MLflow message passed on is its length refusal, such as
  `'value' exceeds the maximum length of 20000 characters`, which names the field and the limit.
- A `StoreError` does not keep the error it was raised from, not even as a suppressed context,
  so no traceback or error reporter reaches the values. The names above are what an operator
  debugs with.
- The work queue (`SqlWorkQueue`, in the record store's database) maps its refusals the same way:
  `put: the database refused a value for martian_work_queue (...)`. Holding an entry that is in the
  queue already raises `AlreadyHeld`, a `conflict` `StoreError` of the same kind, never the
  driver's `IntegrityError`, which Postgres fills with the entry's id and organisation.
- The stores' and the queue's database engines hide statement values (SQLAlchemy's `hide_parameters`), so a
  database failure that is not a refusal holds none either. The record store raises such a
  failure, a lost connection for example, as SQLAlchemy's own error, and MLflow logs some itself,
  such as SQLite's "database is locked". The queue raises such a failure as SQLAlchemy's own error too. What they carry is the statement, with placeholders, and
  the database's own message for the failure.
- Not covered: MLflow shortens a trace tag value over 8,000 characters given to
  `EvalStore.set_trace_tag` and logs a warning quoting its first 100 characters. No route sets a
  trace tag. Errors that are not the stores', such as a function target's own exception, which a
  case result keeps as its error, are the target's text.

`tests/evals/test_store_errors.py` writes a canary in writes that are refused: a trace the
database refuses, directly for a metadata value over 8,000 characters and through a run of the
worker for spans holding the prompt, which a trigger refuses, a tag key and a description MLflow
refuses, a case result, a run two writers create at once, a draft case through its route, and a
score whose write fails. `tests/evals/test_queue_errors.py` does the same for a duplicate `hold` and
a kind the database refuses, in the queue's errors and in an API answer carrying them. Each finds
the canary in no error, API answer or log line, MLflow's included, on SQLite and on Postgres.

## Runtime reporting data boundary

Optional `RuntimeReporter` uses a different destination and credential from the probe APIs above: outbound HTTPS to the backend's `/v1/discovery/telemetry-bindings/{id}/resource-events` (v3), authenticated with a file-mounted, binding-scoped `mt_` token. It sends generated event/trace/optional parent UUIDs, model/tool kind, configured provider or connection alias, configured model/tool name, completion time, duration and outcome. It does not export the assessment TraceRecorder, prompts, arguments, results, customer/session identifiers, raw URLs or provider credentials.

Strict allowlisted fields and identifier syntax limit the payload but cannot detect sensitive data deliberately placed in an identifier. The application owns safe naming and separates production and assessment bindings. The backend derives tenant/deployment/environment/version from the credential; these are declared labels, not hardware or binary attestation. Reports remain application assertions. Local buffers are volatile, bounded and single-attempt; failures and drops are counted locally, not reported as a complete backend capture. Backend time-window queries are not a retention policy; automatic expiry is not implemented. See [lifecycle and delivery limits](customer-integration.md#optional-runtime-reporting).

## API keys

An owner/admin issues one organisation key with explicitly selected evals/CI
and/or `redteam:run` capabilities and applicable resource restrictions. The
plaintext is shown once; save it in a secret manager. Lists and later reads
contain metadata only. The key cannot manage the console or mint other keys.
Existing evals tokens retain their original capabilities; use a newly authorized
key to add redteam. Server-side revocation/lifecycle checks apply on each call.
Model entitlement and per-key operational limits remain server-owned.

Legacy direct `martian_redteam.Client` / `martian-redteam` is an explicit operator
compatibility path only. It requires a complete explicit `Settings` pair or
`MARTIAN_REDTEAM_API_URL` / `MARTIAN_REDTEAM_API_KEY`, never generic platform
variables or `.env`. Its key is bound to that selected origin and YAML cannot
change it. Never reuse a platform token as an Agents key. The legacy development
HTTP escape hatch does not apply to modern `MartianConfig`.

The single-key contract applies to evals/redteam APIs. Target-provider, MCP,
runtime reporting and cooperative call-gate credentials have separate scopes.
No hosted/customer readiness is established merely by installing the wheel;
qualify the coordinated services and auth/resource isolation before rollout.

## Retention and AUP

Operation payloads/results and native execution data follow the **service** retention and recovery policy. Backend/Agents must qualify database/checkpoint/Redis cleanup together; SDK local timeouts do not prove server payload deletion. Toxicity and illegal-activity categories will produce disallowed content by design. Requirements on the service side:

- Acceptable-use statement for stored adversarial payloads
- Bedrock-refusal path treated as an **error**, not a silent pass
- Retention/deletion story before public tenants

The SDK accepts only typed operation results. It does not filter attack text or use a provider refusal as a passing judgment.

## Supply chain and CI hygiene

- Pin GitHub Actions by SHA in this repo and in `examples/github-actions.yml`
- `pull_request_target` only in `.github/workflows/cla.yml`, which never checks out or runs the
  pull request's code ([CLA check](development.md#contributor-license-agreement-check))
- `uv.lock` is required in CI (`uv sync --locked`)
- `persist-credentials: false` on checkout
- Contents-read permission on the example workflow
- No `continue-on-error` on the assessment step

## MLflow trace-id path traversal

The `evals` extra pins `mlflow-skinny` 3.18.0, the first release with both fixes below.

- **The advisory.** CVE-2026-53995 ([GHSA-wqmh-qccw-pqwf](https://github.com/advisories/GHSA-wqmh-qccw-pqwf),
  high) covers MLflow's file-based tracking store only. A trace id such as `../../x` sent to
  `POST /api/3.0/mlflow/traces` made `FileStore.start_trace` write trace files outside the
  experiment's folder. The advisory names 3.17.2 as the first fixed release, but its fix
  ([mlflow#26842](https://github.com/mlflow/mlflow/pull/26842),
  [mlflow#26979](https://github.com/mlflow/mlflow/pull/26979)) is not in 3.17.2; it ships in 3.18.0.
- **Not exposed: the SDK uses SQL stores.** The in-process store, the pytest kit and the suite
  runner use SQLite or Postgres, never the file store. A suite file's `mlflow.tracking_uri` can
  name a file store, but then MLflow tracing in the customer's own process chooses the trace ids.
- **The SQL-store twin.** Up to 3.17.2 the SQL store also accepted any trace id and wrote it into
  the trace's artifact location, so `../../x` pointed the trace at a folder outside its
  experiment. 3.18.0 refuses such an id
  ([mlflow#27311](https://github.com/mlflow/mlflow/pull/27311)); the advisory does not mention it.
  `MlflowStore` is not exposed on any release: it gives MLflow only ids of MLflow's own form
  (`tr-` and 32 hex digits) and derives one from any other id.
  `tests/evals/test_mlflow_compat.py` checks both the refusal and the derived ids.
- **The hosted MLflow server** (MartianSecurity/martian-evals) runs its own pinned release on Postgres,
  not the file store. Only Backend holds credentials for it, and Backend sends only derived
  `tr-` ids; MartianSecurity/MartianBackend's evals workbench guide states that side.

## Licence, attribution and vulnerability reports

Every file in this repository is Apache-2.0 (`LICENSE`) except the third-party code bundled in the
Evals UI build in `src/martian/ui/`: its JavaScript bundles hold third-party packages under MIT, ISC
and Apache-2.0, and its Inter font files are OFL-1.1, each listed with its licence text in
`src/martian/ui/third-party-licenses.md`. The UI's own code is Apache-2.0.
Paid Martian features are not in this repository. Report a vulnerability as
[SECURITY.md](../SECURITY.md) describes, never in a public issue.

`NOTICE` records DeepTeam as the source of taxonomy identifiers and labels, and the Evals UI's packages with the MIT notice of the shadcn/ui code in them. The simulation and evaluation prompts (`martian.redteam.catalog.prompts`, which martian-agents imports) are authored by Martian Security and are not copies of DeepTeam template corpora. Do not paste DeepTeam metric class files into this tree.

### Red team content licences

`src/martian/redteam/catalog/licences.json`, which ships in the package, is the register of red team
content: each prompt set, catalogue file, dataset, attack algorithm and interop map, with its source,
licence and whether it allows commercial use. Content may ship under Apache-2.0, MIT, BSD-2-Clause,
BSD-3-Clause, CC0-1.0, CC-BY-4.0 or the Unlicense, and only when commercial use is allowed.

A content file is a file Git tracks that matches the register's `content.globs`: the catalogue
(`martian.redteam.catalog`, `martian_redteam.catalog`), attack methods (`martian.redteam.methods`),
the interop maps, the verdict test vectors, the attacker refusal phrases, the encodings, and any data
file (`.json`, `.jsonl`, `.csv`, `.txt`, `.yaml`, `.parquet`) under `martian/redteam` or
`martian_redteam`, less the API contract. `scripts/content_licences.py` fails when a content file has
no entry, an entry names a file that is gone, or an entry's licence is not permitted or does not allow
commercial use; `tests/redteam/test_content_licences.py` runs it in CI. New content needs its entry
in the same change.

The register also lists sources that do not ship. BeaverTails (CC BY-NC 4.0) and Do-Not-Answer
(non-commercial) may not enter the package, and neither may text derived from the OWASP Machine
Learning Security Top 10 (CC BY-SA 4.0). `MartianSecurity/martian-agents`, `martian-skills` and
`martian-wiki` have no licence file, and some guidance skills are built from those restrictive
sources; they are recorded as not opened, and nothing from them enters this package until they have a
licence and their content is registered.

## Resource runtime metadata

The optional runtime reporter can export explicitly configured response-ID mappings under the v3 contract. A successful MCP structured response contributes at most ten opaque resource references with configured service/type/action, completion time and reported outcome. It does not export native IDs, arguments or response bodies. References are keyed, binding-scoped HMAC values; they are pseudonymous correlation data, not anonymous data or authorization proof. Missing/error/unmapped calls have unknown resource coverage. Credentials and references change with a new deployment binding. See [configuration and boundaries](customer-integration.md#explicit-resource-response-mappings).

The separate explicit `native_kubernetes=True` mode exports v4 to `/native-events`.
It includes native cluster alias, service-account namespace/name and named request
coordinates. Those identifiers are **not opaque** and may reveal infrastructure
metadata; customer approval of names and binding scope is required. It exports no
credential/header/body/response and does not inspect an existing Kubernetes client.
The application declares the actual identity and request, including on errors.
See [opt-in usage](customer-integration.md#explicit-kubernetes-request-reporting).

## Opt-in call control boundary

The separate `mg_` gate credential exposes only configured target identifiers and
generated process/request UUIDs. It cannot authorize assessments, telemetry, console
reads or control administration. A customer can attach an exact target assignment
to a reporter; selected calls then fail closed without a live decision. This changes
application availability intentionally and remains cooperative instrumentation,
without complete-replica attestation or resistance to direct SDK bypass. Telemetry
revocation and gate revocation have different effects. See the [full lifecycle and
limits](customer-integration.md#sdk-call-gate).
