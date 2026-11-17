# Red team interop

Bring red team work from other tools. Nothing here installs or calls the other tool's service.

| Tool | What comes over | Command |
| --- | --- | --- |
| promptfoo | A red team config, as a [version 2 run file](redteam-run-file.md) | `martian redteam convert promptfoo` |
| promptfoo | Red team results, as runs in the local store that `martian ui` opens | `martian redteam import promptfoo` |

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
| `base64`, `rot13`, `leetspeak` | `Base64`, `ROT13`, `Leetspeak` |
| `prompt-injection` | `PromptInjection` |

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
| `jailbreak`, `jailbreak:meta`, `jailbreak:tree`, `jailbreak:composite`, `crescendo`, `goat`, `jailbreak:hydra`, `best-of-n`, `math-prompt` and other adaptive or model-written strategies | This release's attack methods are prompt injection, roleplay, prompt probing and three encodings |
| `hex`, `homoglyph`, `morse`, `jailbreak-templates`, `multilingual` and other transforms | No attack method in the catalogue |
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
