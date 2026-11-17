# Moving from promptfoo red team

What carries over from a promptfoo red team project, what does not, and how to bring it. promptfoo
need not be installed. Every mapping is listed in [red team interop](redteam-interop.md).

## The concepts

| promptfoo | Martian |
| --- | --- |
| The `redteam` section of `promptfooconfig.yaml` | A [version 2 run file](redteam-run-file.md) |
| `targets` (or `providers`): an HTTP provider, a Python provider | `target`: an HTTP endpoint (`url`), a Python function (`file`), or an MCP server (`mcp`) |
| `redteam.purpose` | `target.purpose` |
| `redteam.provider`, which generates and grades | `models.attacker` and `models.judge`, called with your own key |
| A plugin, such as `pii:direct` | A vulnerability and its types, such as `PIILeakage` `direct_disclosure` (`martian redteam catalog`) |
| The `policy` plugin | A custom vulnerability with `criteria` |
| A strategy, such as `base64` or `crescendo` | An attack method, such as `Base64` or `Escalation` ([attack methods](redteam-methods.md)) |
| `numTests`, `maxConcurrency` | `budget.attempts_per_probe`, `budget.max_concurrent`; the budget also caps target queries, model calls, tokens, cost and time |
| `promptfoo redteam generate`, then `eval` of the generated `redteam.yaml` | One `martian redteam run`: plan, attack and verify, with the run kept after every attempt and resumable. No generated test file |
| The plugin's grader, a model reading the reply | A `judge` check, which is evidence level 1, suspected. Checks stated in advance decide on evidence: `text` and `canary` (level 2), `tool_call` (level 3) and `receipt` (level 4), confirmed ([verdicts](redteam-verdicts.md)) |
| `graderExamples`, `graderGuidance` | Checks stated in advance, and a custom vulnerability's `criteria` |
| Pass rate, `PROMPTFOO_PASS_RATE_THRESHOLD`, exit code 100 | The [gate](redteam-run-file.md#gate): fail or warn by evidence level and severity, and what undetermined attempts and errors do; exit codes 0, 1 and 2 |
| The `retry` strategy | `martian redteam pin add` and `pin run`: confirmed findings re-run with no model |
| `promptfoo view` and the report page | `martian ui`: the Red Team pages |
| Export to CSV, JSON or PDF | `-f html,csv,sarif,junit`, beside JSON and Markdown ([reports](redteam-reports.md)) |
| Remote generation through promptfoo's API, and its email | None. A run with `models` calls only your model and your target ([what a local run connects to](security-and-data.md#what-a-local-red-team-run-connects-to)) |

## What imports and what does not

- **Configs convert** (`martian redteam convert promptfoo`): an HTTP target with a JSON body, its
  headers with `{{env.NAME}}`, a `json.…` response path; the purpose; an `openai:` provider; 27
  plugins onto the catalogue's 12 vulnerabilities, and policies; the encodings, `prompt-injection`,
  `citation`, `jailbreak:tree` (as `TreeSearch`), `crescendo` (as `Escalation`) and `best-of-n` (as
  `Mutation`); `numTests` and `maxConcurrency`.
- **Results import** (`martian redteam import promptfoo`) as runs in the local store that `martian ui`
  opens. Every hit stays suspected: promptfoo's grader is a model that read the reply.
- **Not converted, and listed with the reason** in the command's output and at the top of the run
  file: plugins with no vulnerability in the catalogue (such as `bola`), dataset plugins, `intent`,
  promptfoo's service-run strategies (`jailbreak:meta`, `jailbreak:composite`, `goat`,
  `jailbreak:hydra` and the rest), other transforms, media, request and response code
  (`transformRequest`, `transformResponse` functions), session parsers, auth and TLS settings, and
  a promptfoo Python provider, which has another signature: give `--target FILE.py:FUNCTION`.
- `basic` needs nothing: every attack method writes its own attack for the vulnerability. A config
  whose strategies none convert, including promptfoo's default, needs `--attack`.

## Walk through the sample project

[`examples/promptfoo-redteam`](../examples/promptfoo-redteam) has a promptfoo red team config for a
help desk, its results, and `agent.py`, a stand-in for the help desk that answers from rules. From
that folder:

<!-- from-promptfoo commands -->
```bash
martian redteam convert promptfoo promptfooconfig.yaml --output converted
martian redteam validate converted/redteam.yaml
martian redteam convert promptfoo promptfooconfig.yaml --target agent.py:reply --attack PromptInjection --output local
martian redteam run local/redteam.yaml
martian redteam import promptfoo results.json
```

1. The first command converts the config as it is: the HTTP target at `localhost:8300`, five
   vulnerabilities, `Base64` and `Escalation` (from `crescendo`). It lists `bola`, `basic`,
   `jailbreak:composite` and `language` as not converted.
2. `validate` checks the run file without calling anything.
3. The second conversion attacks `agent.py`'s `reply` function instead, and adds `PromptInjection`, a
   single written attack in place of promptfoo's `basic` tests.
4. `run` attacks the function with the model in `redteam.provider` (`openai:/gpt-4.1-mini`), with
   `OPENAI_API_KEY` and, for another endpoint, `OPENAI_API_BASE` set ([quickstart](redteam-quickstart.md#2-point-it-at-a-model)).
   Its findings are suspected, since the converted vulnerabilities have no checks stated in advance,
   so the default gate passes. Add a `text` check for what must never appear, such as the help
   desk's instructions, to confirm a leak.
5. `import` keeps promptfoo's own results as a run beside it. `martian ui` shows both.

`tests/redteam/test_from_promptfoo_doc.py` runs these commands from this page against the sample, with
a loopback model.
