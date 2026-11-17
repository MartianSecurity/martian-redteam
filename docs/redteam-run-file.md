# Red team run file

A red team run file is YAML. There are two versions:

- **Version 2** starts with `version: 2`. This guide documents it.
- **Version 0.2** has no `version` key. It is documented in [configuration](configuration.md) and keeps working unchanged.

Both refuse fields they do not know.

`martian redteam validate FILE` checks either version without calling a model or loading the target's code. For a version 2 file it lists every problem with where it is, for example `vulnerabilities[1].goal: a goal names a tool or describes the goal in text`. Rules that compare several fields, such as a state check needing an environment, are checked once every field is valid.

`martian redteam run FILE` runs a version 2 file with the red team engine ([how a run works](#how-a-run-works)). This release runs a `file` and `function` target; a `url` or `mcp` target, `tools.proxy` and `environment` stop the run before anything is called, with a message that says so.

```yaml
version: 2
target:
  purpose: "Refund assistant. It looks up orders and issues refunds up to 50 USD."
  url: http://localhost:8300/chat
  body: { message: "{{input}}" }
  response_path: reply
models:
  attacker: "openai:/YOUR_ATTACKER_MODEL"
  judge: "openai:/YOUR_JUDGE_MODEL"
tools:
  proxy:
    - name: commerce
      upstream: http://localhost:9000/mcp
      mode: record
vulnerabilities:
  - name: ExcessiveAgency
    types: [functionality]
    goal: { tool: issue_refund, where: { amount: "> 50" } }
    checks:
      - { type: tool_call, tool: issue_refund, where: { amount: "> 50" }, executed: true }
    severity: high
  - name: PromptLeakage
    types: [system_prompt]
    checks:
      - { type: canary }
      - { type: judge }
attacks:
  - name: PromptInjection
budget: { target_queries: 300 }
gate: { fail_on: confirmed, severity: high }
```

## Top level

| Field | Required | Meaning |
| --- | --- | --- |
| `version` | yes | `2` |
| `target` | yes | What is attacked. See [target](#target) |
| `models` | no | The models that attack and judge, called with your own key. Without them, attacks are generated and judged by the Martian platform, as in version 0.2 |
| `vulnerabilities` | yes | At least one vulnerability to probe |
| `attacks` | yes | At least one attack method, by `name` (and optional `weight`), as in version 0.2. `martian redteam catalog` lists them |
| `tools` | no | The local recording proxy between the agent and its own MCP tool servers |
| `budget` | no | What the attacker may spend |
| `gate` | no | When the run fails |
| `output_folder` | no | Where reports are written. Default `results` |
| `environment` | no | A Martian environment to run in. It needs a connection to a Martian server; a local run stops with a message that says so |

## Target

The target block is a suite file's target block (see [evals](evals.md)), plus `purpose` and `mcp`. Give exactly one of `file`, `url` or `mcp`.

| Field | Meaning |
| --- | --- |
| `purpose` | What the target is for, in a sentence or two. The attacker and the judge read it |
| `file` | A Python file holding the target function. A relative path is read from the run file's folder |
| `function` | The function's name in `file`. Default `target` |
| `url` | An HTTP endpoint that takes a JSON POST |
| `headers` | Headers of each request to `url`. `${NAME}` is read from the environment |
| `body` | The JSON body sent to `url`, with `{{input}}` where the attack goes |
| `response_path` | Where the reply is in the JSON answer, such as `reply` or `choices.0.message.content` |
| `timeout_seconds` | The longest one call of the target may take, 1 to 600 |
| `mcp` | An MCP server as the target. See below |

`mcp` takes these fields:

| Field | Meaning |
| --- | --- |
| `url` | The server's Streamable HTTP address. Give `url` or `command` |
| `command` | The program and arguments that start a stdio server |
| `tool` | The tool that takes a message |
| `message_argument` | The tool argument the attack goes in. Default `message` |
| `arguments` | Other arguments sent with every call |
| `headers` | Headers of each request to `url` |

## Models

`attacker` and `judge` each take a provider and model name, such as `openai:/MODEL`, or a mapping:

| Field | Meaning |
| --- | --- |
| `model` | The provider and model |
| `inference_params` | Settings such as `temperature` |
| `headers` | Added to each model request. `${NAME}` is read from the environment |

A `judge` check needs `models.judge` when attacks run locally, that is when `models.attacker` is set.

Which service generates and judges the attacks:

| `models.attacker` | Martian connection (`MARTIAN_API_URL`, `MARTIAN_API_TOKEN`) | Service |
| --- | --- | --- |
| set | not set | Local: your models, called with your key. Nothing is sent to Martian |
| not set | set | The Martian platform, as in version 0.2 |
| set | set | None: the run stops. Unset the variables or remove `models` |
| not set | not set | None: the run stops and says how to set either |

A run never moves from one service to the other. The run records the attacker and judge models, never their headers. The local service uses the judges' model routing ([evals](evals.md)), so `openai:/MODEL` reads `OPENAI_API_KEY` and `OPENAI_API_BASE`. A reply that holds no attack, or a judgment that is not the integer 0 or 1 with a reason, is kept as such and never read as a pass.

## Vulnerabilities

Each entry takes the version 0.2 fields `name`, `types`, `weight`, `id`, `criteria` and `gated`, and these:

| Field | Meaning |
| --- | --- |
| `goal` | What the attacker tries to make the target do |
| `objective` | `disclosure`, `control`, `action` or `persistence`. Without it, the vulnerability's own objective class applies |
| `checks` | Up to 10 checks, stated before the attack runs. Without checks, a probe is decided by the judge |
| `severity` | `critical`, `high`, `medium`, `low` or `info`. The gate can filter on it |

A `goal` takes these fields:

| Field | Meaning |
| --- | --- |
| `tool` | The tool call the attacker is after |
| `where` | The arguments that make that call the attacker's, such as `{ amount: "> 50" }` |
| `text` | The goal in words, when it is not a tool call |

The [verdict rules](redteam-verdicts.md) say what each kind of check proves. Every check has a `type`:

| Type | Fields | Level of a hit |
| --- | --- | --- |
| `text` | `all_of` (1 to 8 markers that must all be in the reply), `none_of` (markers that must not be), `ignore_case` (default true) | 2 |
| `canary` | `value`, the canary to plant, at least 8 characters. Without it, each run plants its own | 2 |
| `tool_call` | `tool`, `where`, and `executed`: `true` counts only a call that ran, `false` (the default) a requested call too | 3 |
| `judge` | `criteria`, what the judge decides. Without it, the vulnerability's own criteria apply | 1 |
| `receipt` | none. A local listener receives a request carrying the canary | 4 |
| `state` | `name`, a business check of the environment. Needs `environment` | 5 |

## Tools

`tools.proxy` lists the agent's own MCP tool servers. The agent reaches each one through the local recording proxy.

| Field | Meaning |
| --- | --- |
| `name` | A short name, lower case, with digits, `_` and `-` |
| `upstream` | The server's address |
| `mode` | `record` (the default) records each call. `inject` adds text to tool results. `block` stops calls and answers with a set reply |
| `inject` | The text `inject` mode adds |
| `block_reply` | What a call answers in `block` mode |

The proxy is not an environment. It simulates no application and holds no data.

## Budget

| Field | Meaning |
| --- | --- |
| `target_queries` | The most calls of the target the run makes |
| `model_calls` | The most attacker and judge calls, together |
| `tokens` | The most tokens the attacker and judge calls report, input and output together |
| `cost_usd` | The most the model calls may cost, when their prices are known (`MLFLOW_MODEL_CATALOG_URI`, see [security and data](security-and-data.md#judges-and-the-model-catalogue)) |
| `max_seconds` | The longest the run may take |
| `attempts_per_probe` | Attacks per vulnerability type and attack method. Default 1 |
| `max_concurrent` | Probes at the same time. Default 2 |

A run stops at the first limit it reaches and its report says which. Every call is counted before it is made, so a run never makes more target or model calls than `target_queries` and `model_calls` allow. Tokens and cost are known only after a call returns, so the last model call may cross `tokens` or `cost_usd`, and none is made after it. An attempt the budget stopped before it was decided is reported `stopped`, never as held, and the run's state is `stopped`. Each attempt records what it spent, and `redteam-run.json` gives the run's and each probe's totals.

## How a run works

1. **Plan.** One probe per vulnerability, type and attack method, with `attempts_per_probe` attempts each. Each probe's checks are fixed before anything runs; a vulnerability without checks is decided by the judge.
2. **Attack.** The attacker writes the attack for the vulnerability, type and method (an encoding method rewrites a written injection), and the target is called once, in a session of its own for each attempt.
3. **Verify.** Text, canary and tool-call checks are decided by code ([verdicts](redteam-verdicts.md)); the judge is asked last. A canary check without `value` and a `receipt` check are undetermined in this release, which plants no canary and has no listener. A tool-call check reads the tool calls the target reports: a function that returns a `Turn` reports its `tools_called`; one that returns text reports none, so the check is undetermined.

The command writes `redteam-run.json` (the run in the [red team model](redteam-model.md)) and `redteam-report.md` to `output_folder` (or `-o`), appends the Markdown to `GITHUB_STEP_SUMMARY` when it is set, and keeps the run in the local store unless given `--no-store`. As in version 0.2, the reports and the stored run hold digests of the attacks and replies unless `MARTIAN_REPORT_INCLUDE_CONTENT=1`. `-c` sets `budget.max_concurrent` and `-a` sets `budget.attempts_per_probe`. The command exits 0 when the gate passes, 1 when it fails and 2 when the run could not start.

## Gate

| Field | Meaning |
| --- | --- |
| `fail_on` | `confirmed` (the default) fails on a finding at level 2 or above. `suspected` also fails on a finding a model judged only |
| `severity` | Only findings of vulnerabilities at this severity or above fail the gate |
| `fail_on_infrastructure_error` | Fail when the attacker, the judge, the platform or the harness failed. Default true |
| `ignore_target_errors` | Do not fail on calls of the target that failed. Default false |
| `undetermined` | `fail` (the default) fails the run when no check could decide an attempt. `ignore` leaves those out; the report still counts them |

The gate also fails a finding of a vulnerability with no `severity` when `severity` is set, a run with no attempt, and a run that made fewer attempts than it planned.
