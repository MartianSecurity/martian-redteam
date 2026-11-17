# Red team interop

Bring red team work from other tools. Nothing here installs another tool or calls its service.

| Tool | What comes over | Command |
| --- | --- | --- |
| promptfoo | A red team config, as a [version 2 run file](redteam-run-file.md) | `martian redteam convert promptfoo` |
| promptfoo | Red team results, as runs in the local store that `martian ui` opens | `martian redteam import promptfoo` |
| garak | Its probes, sent to a run file's target, with the results as a run | `martian redteam garak` |
| garak | A garak report, as a run in the local store | `martian redteam import garak` |

[Moving from promptfoo red team](redteam-from-promptfoo.md) walks through both with the sample
project in [`examples/promptfoo-redteam`](../examples/promptfoo-redteam).

## promptfoo configs

```bash
martian redteam convert promptfoo promptfooconfig.yaml --output converted
martian redteam convert promptfoo promptfooconfig.yaml --target agent.py:reply \
  --attack PromptInjection --output converted          # a Python function as the target, one more attack
martian redteam validate converted/redteam.yaml
```

The converter (`martian.redteam.interop.promptfoo`) reads a promptfoo config that has a `redteam`
section and writes `redteam.yaml` into `--output`. It prints what converted and everything that did
not, with the reason, and writes the same list as comments at the top of the file. It never runs
promptfoo or calls a model. It stops with exit code `2` and writes nothing when the config has no
`redteam` section, when no target, plugin or strategy converts, when the config has no `purpose` and
`--purpose` is not given, or when `--output` already has a `redteam.yaml`.

| promptfoo | Run file |
| --- | --- |
| `targets` (or `providers`): the first `http` or `https` provider, or one whose id is its URL | `target.url`. `config.body` (an object or JSON text) with the attack variable, `{{prompt}}` or `injectVar`, as `{{input}}` and `{{sessionId}}` as `{{session_id}}`; `config.headers` with `{{env.NAME}}` as `${NAME}`; `config.transformResponse` as `response_path` when it only reads a path of `json`, such as `json.choices[0].message.content`; `config.timeout` in milliseconds as `timeout_seconds` |
| `--target FILE.py[:FUNCTION]` | `target.file` and `target.function`, relative to the run file. It replaces the config's target |
| `redteam.purpose`, else `--purpose` | `target.purpose` |
| `redteam.provider`: `openai:chat:MODEL` or `openai:MODEL`, with `temperature` and `max_tokens` | `models.attacker` and `models.judge`, both `openai:/MODEL`. promptfoo uses one model for both |
| `redteam.plugins` | `vulnerabilities`, by the table below. Plugins that map to one vulnerability share it and its types; a plugin's `severity` (`informational` as `info`) becomes the vulnerability's, the highest when several plugins share it |
| `policy` plugin with `config.policy` | A custom vulnerability, `id: policy-N`, whose criteria are the policy's text |
| `redteam.strategies` | `attacks`, by the table below, then each `--attack` |
| `redteam.numTests` | `budget.attempts_per_probe`, at most 100 |
| `redteam.maxConcurrency` | `budget.max_concurrent`, at most 64 |

| promptfoo plugin | Vulnerability and types |
| --- | --- |
| `prompt-extraction` | `PromptLeakage` `system_prompt` |
| `pii:direct`, `harmful:privacy` | `PIILeakage` `direct_disclosure` |
| `pii:session`, `cross-session-leak` | `PIILeakage` `session_leak` |
| `pii:api-db` | `PIILeakage` `database_access` |
| `rbac` | `RBAC`, every type |
| `bfla` | `RBAC` `privilege_escalation` |
| `ssrf`, `sql-injection`, `shell-injection`, `excessive-agency` | `SSRF`, `SQLInjection`, `ShellInjection`, `ExcessiveAgency`, every type |
| `harmful:hate`, `harmful:insults`, `harmful:profanity` | `Toxicity` `hate`, `insults`, `profanity` |
| `harmful:violent-crime` | `IllegalActivity` `violent_crimes` |
| `harmful:cybercrime`, `harmful:cybercrime:malicious-code` | `IllegalActivity` `cybercrime` |
| `harmful:misinformation-disinformation` | `Misinformation` `factual_errors` |
| `harmful:specialized-advice` | `Misinformation` `expertize_misrepresentation` |
| `hallucination` | `Misinformation` `unsupported_claims` |
| `bias:gender`, `bias:race`, `religion`, `politics` | `Bias` `gender`, `race`, `religion`, `politics` |
| `overreliance`, `hijacking` | `Robustness` `input_overreliance`, `hijacking` |

The `pii` and `bias` collections are expanded first; `pii:social`, `bias:age` and `bias:disability`
do not convert.

| promptfoo strategy | Attack method |
| --- | --- |
| `base64`, `rot13`, `leetspeak`, `hex`, `homoglyph`, `morse` | `Base64`, `ROT13`, `Leetspeak`, `Hex`, `Homoglyph`, `Morse` |
| `prompt-injection` | `PromptInjection` |
| `citation` | `Citation` |
| `jailbreak:tree` | `TreeSearch` |
| `crescendo`, with `maxTurns` and `maxBacktracks` | `Escalation`, with `max_turns` and `max_backtracks` |
| `best-of-n` | `Mutation` |

The [attack methods](redteam-methods.md) say what each one does and spends; promptfoo's and ours
are written independently, so the same name is the same idea, not the same prompts.

Listed, not converted:

| promptfoo | Why |
| --- | --- |
| Other plugins, such as `bola`, `harmful:non-violent-crime` and the domain packs | No vulnerability in the catalogue covers them (`martian redteam catalog`) |
| `default` and `harmful` plugin collections | Their members depend on promptfoo's version; name the plugins |
| `intent` | Fixed prompts sent as written; the attacker here writes each attack |
| `indirect-prompt-injection`, `indirect-web-pwn` | Use the [recording proxy](redteam-targets.md#recording-proxy) in `inject` mode to deliver an injection |
| Dataset plugins (`beavertails`, `harmbench`, `donotanswer` and the rest) | promptfoo downloads them when it generates; no dataset ships here, and their licences differ ([content licences](security-and-data.md#red-team-content-licences)) |
| `file://` plugins and strategies | Custom prompts or JavaScript; write a custom vulnerability with `criteria` |
| `basic` | promptfoo's plain plugin tests. Every attack method here writes its own attack, so it needs no method of its own |
| `default` strategy, or no `strategies` | promptfoo's `basic`, `jailbreak:meta` and `jailbreak:composite`; none converts, so give `--attack` |
| `jailbreak`, `jailbreak:meta`, `jailbreak:composite`, `jailbreak:likert`, `goat`, `jailbreak:hydra`, `math-prompt` and other model-written strategies | promptfoo runs most of them on its own service; none has an attack method here |
| `piglatin`, `camelcase`, `emoji`, `jailbreak-templates`, `multilingual` and other transforms | No attack method here |
| `image`, `audio`, `video` | Targets here take text |
| `layer`, `retry` | Convert each layered strategy; pin confirmed findings with `martian redteam pin add` instead of `retry` |
| A plugin's or strategy's other `config` keys, and `language`, `frameworks`, `entities`, `contexts`, `testGenerationInstructions`, `graderExamples`, `delay`, `tracing` | The run file has no such setting |
| A target's `method` other than `POST`, a body that is not JSON, other templates, `transformResponse` code, `transformRequest`, `sessionParser`, `auth` and TLS settings | The [HTTP target](redteam-targets.md) posts a JSON body and reads a path; set them by hand |
| A promptfoo Python provider (`file://agent.py`) | promptfoo calls `call_api(prompt, options, context)`; give `--target` a function that takes the message |
| A model as the target, and targets after the first | Give `--target`; a run file attacks one target |

## promptfoo results

```bash
martian redteam import promptfoo results.json     # as `promptfoo redteam run -o results.json` writes it
martian ui                                        # the run is in the Red Team pages
```

The importer reads the JSON output file promptfoo writes (`promptfoo redteam run -o` or `promptfoo eval
-o`), whose `results` is an `EvaluateSummary` of version 3, the same file shape `martian evals import
promptfoo` reads ([promptfoo results](evals-core.md#promptfoo-results-and-configs)). Every row must be
a red team row, with `testCase.metadata.pluginId` or a `promptfoo:redteam:*` assertion; a file of
eval results is refused with exit code `2` and points to `martian evals import promptfoo`. Every file
is read before anything is written.

Each provider in the file is one [`RedteamRun`](redteam-model.md) in the local store (`--data-dir`,
else `MARTIAN_EVALS_HOME`, else `~/.martian/evals`), with id `promptfoo-<evalId>-<provider>`.
Importing the file again replaces that run. The run is kept as `martian redteam run` keeps one: the
attacks, replies and grader reasons as digests unless `MARTIAN_REPORT_INCLUDE_CONTENT=1`, and
credentials redacted ([security and data](security-and-data.md)).

| promptfoo | Red team model |
| --- | --- |
| A row's plugin and `strategyId` (`basic` when absent) | A probe. A mapped plugin and strategy take the catalogue's vulnerability, type and attack method (the tables above); others keep promptfoo's id as `promptfoo:<id>`. `Probe.metadata["promptfoo"]` keeps both ids |
| `testCase.metadata.goal`, `pluginConfig.policy` | `Probe.goal`, and a policy probe's `criteria` |
| A row | An `Attempt`. Its input is the attack (`vars[injectVar]`), its output the target's reply, and its turns `response.metadata.redteamHistory` when promptfoo kept one |
| `success` true | `resisted` |
| `success` false | `hit` at level 1, suspected. The grader's verdict is the attempt's one check, `promptfoo-grader` of kind `judge`, with promptfoo's reason |
| `failureReason` 2 | `error`: a `target` error when `response.error` is set, else a `judge` error |
| `testCase.metadata.severity` | The finding's severity |
| `evalId`, the file's path and SHA-256, the timestamp, the provider, `metadata.promptfooVersion` | `RedteamRun.provenance["promptfoo"]` |
| `redteam.purpose`, else a row's `metadata.purpose` | `RedteamRun.purpose` |

Every imported hit is suspected, never confirmed: promptfoo's graders are models that read the reply,
the checks were not stated before the attack, and nothing was re-run. The run's `limitations` say so,
and its catalogue version is `promptfoo`. Re-run the converted config to get findings this engine
decides.

## garak probes

[garak](https://github.com/NVIDIA/garak) (Apache-2.0) has the widest set of fixed probes. It is not
bundled and not a dependency: install it in the same environment (`pip install garak`), which brings
its own large dependencies, such as PyTorch.

```bash
martian redteam garak redteam.yaml --probes encoding                    # garak's probes, this run file's target
martian redteam garak redteam.yaml --probes dan.DanInTheWild --generations 3 --garak-arg=--seed --garak-arg=1
```

`martian redteam garak` runs `python -m garak` with garak's `function` generator pointed at
`martian.redteam.interop.garak#generate`. That function sends each prompt to the run file's target in
a conversation of its own and returns the reply. garak's report goes to `garak.report.jsonl` in the
output folder (`-o`, else the run file's `output_folder`). The command then reads the report as a red
team run, writes `redteam-run.json`, `redteam-report.json` and `redteam-report.md` there, keeps the run
in the local store unless given `--no-store`, and applies the run file's [gate](redteam-run-file.md#gate):
exit code `0` when it passes and `1` when it fails.

Without garak the command is skipped with a message that says so and exits with `2`, as a run that
could not start does. A run file with `environment` or `tools.proxy` is refused: garak calls the
target directly. A function, HTTP or MCP target works.

Without the command, run garak yourself and import its report:

```bash
MARTIAN_GARAK_RUN_FILE=redteam.yaml MARTIAN_GARAK_LEDGER=calls.jsonl \
  garak --target_type function --target_name martian.redteam.interop.garak#generate --probes encoding
martian redteam import garak ~/.local/share/garak/garak_runs/garak.<run id>.report.jsonl \
  --run-file redteam.yaml --ledger calls.jsonl
```

| Variable | What `generate` reads |
| --- | --- |
| `MARTIAN_GARAK_RUN_FILE` | The version 2 run file whose target garak attacks. Required |
| `MARTIAN_GARAK_LEDGER` | A file to append each call to: the prompt, the reply and the tool calls the target reported, readable by its user only. A tool-call check needs it |

How a garak report becomes a [`RedteamRun`](redteam-model.md):

| garak | Red team model |
| --- | --- |
| A probe class, such as `encoding.InjectBase64` | A probe: vulnerability `garak.encoding`, type `InjectBase64`, attack method `garak`. `Probe.metadata["garak"]` keeps the class and its intent |
| An evaluated attempt (`status` 2) and each of its outputs | One `Attempt` per output, its input the prompt's last user message |
| A detector's score of that output | A check result `garak:<detector>` of kind `judge`: a hit at level 1, suspected, at or above 0.5 (garak's default threshold); undetermined when there is no score |
| The run file's `text` checks, `canary` checks with a `value`, and `tool_call` checks | Applied to every output. A hit is confirmed at the check's level: 2 for text and canary, 3 for a tool call, which reads the calls the ledger recorded |
| An output that is empty (`null`) | An `error` of kind `target` |
| `transient.run_id`, `transient.starttime_iso`, `_config.version` | The run's id `garak-<run id>`, its start and `provenance["garak"]["version"]` |

garak's detectors are reported as suspected because they are not the checks this run file stated. The
run file's `judge` checks are not applied: no model is called. The run is kept with its content as
digests, as `martian redteam run` keeps one. garak's own report and hit log hold the prompts and
replies in clear text; the ledger is removed when `martian redteam garak` finishes.
