# Red team reports

A red team report says what attacked, with what budget, and how strong each result is. Attack
success as one number cannot be compared between runs: it depends on the attacker, the budget and
the judge. So every report states all three, and keeps confirmed and suspected results apart.

`martian.redteam.report.build_report` computes the report from a run in the
[red team model](redteam-model.md). `martian redteam run` writes it to `redteam-report.json` beside
`redteam-run.json` (the run itself) and `redteam-report.md`, and the API serves it at
`GET /v1/redteam/runs/{run_id}/report` ([red team API](redteam-api.md)).

## Rules every number follows

- **Attack attempts only.** Counts and rates are of attempts whose `role` is `attack`. Repetitions
  and negative controls are reported under [verification](#verification-and-controls).
- **Decided attempts are the denominator.** An attempt is decided when it was a hit or the target
  resisted. Undetermined, stopped and errored attempts are not in any rate; they are counted beside
  it, as outcomes of their own.
- **Confirmed, attempted and suspected stay apart.** A hit at evidence level 2 or above is confirmed,
  except one whose strongest evidence is a tool call that was asked for and is not known to have run:
  that is an attempted action. A hit at level 1, which a model judged only, is suspected
  ([verdicts](redteam-verdicts.md)). They are never added together.
- **A refused generation is never a resisted attack.** When the attacker refuses twice, the attempt
  is an `attacker` error, and the target is not called.
- **Intervals.** Every rate has a two-sided 95% Wilson score interval (`martian.evals.stats.wilson`).
- **A zero count is an upper bound.** With no success, a rate has no point value. For example, 0 of
  140 is shown as `≤2.7% (0 of 140, 95% upper bound)`, never as 0%.

## A rate

Every rate in the report is an object with these fields:

| Field | Meaning |
| --- | --- |
| `successes` | How many succeeded |
| `trials` | Out of how many |
| `rate` | `successes / trials`. `null` with no trial and with no success |
| `low`, `high` | The 95% Wilson interval. `null` with no trial |
| `upper_bound` | With no success, `high`: the most the rate can be at 95% confidence. Otherwise `null` |
| `display` | The text every output shows: `30% (10–61%), 3 of 10`, `≤2.7% (0 of 140, 95% upper bound)` or `no decided attempts` |

## The report

`redteam-report.json` has `version` `martian-redteam-report-v1` and these fields.

### The run

| Field | Meaning |
| --- | --- |
| `run_id`, `project_id`, `source`, `state` | The run's id, project, where it came from (`local`, `console`, `ci`, `campaign`) and how it ended |
| `started_at`, `finished_at`, `duration_seconds` | When it ran |
| `catalog_version`, `rules_version`, `prompts_revision`, `engine_version` | The catalogue, the [verdict rules](redteam-verdicts.md), the attacker and judge prompts and the engine that made it |
| `target` | `kind` and `name` of the target. No credential and no purpose text |
| `limitations` | What the run could not establish, in words |
| `gate` | The gate's result when the run applied one: `passed`, the `failures` and `warnings`, and the `config` it used. `null` otherwise ([gate](redteam-run-file.md#gate)) |

### What attacked

| Field | Meaning |
| --- | --- |
| `service` | `local` (your models) or `hosted` (the Martian platform) |
| `attacker`, `judge` | Each model's `model` (provider and name) and `inference_params`. `null` when the run did not record one, as with the hosted service |
| `judge_used` | Whether any probe had a judge check |
| `methods` | The attack methods, in plan order |
| `budget` | `limits` (the run file's budget), `spent` (target queries, attacker and judge calls, tokens and cost), `stopped_by` (the limit that stopped the run, or `null`) and `attempts_per_probe` (the most attempts a probe planned) |

### Outcomes

`outcomes` counts the attack attempts:

| Field | Meaning |
| --- | --- |
| `attempts` | Every attack attempt |
| `decided` | `hits` plus `resisted` |
| `hits` | Attempts a check found vulnerable |
| `confirmed_hits` | Hits at level 2 or above, attempted actions excepted |
| `attempted_hits` | Hits whose strongest evidence is a tool call not known to have run |
| `suspected_hits` | Hits at level 1 only |
| `resisted` | Attempts every check decided and none hit |
| `undetermined` | Attempts no check could decide |
| `not_delivered` | Of the undetermined, attempts whose delivery route never put the payload where the agent read it ([delivery routes](redteam-methods.md#delivery-routes)): the agent was not tested. The Markdown report says how many |
| `stopped` | Attempts the budget stopped before they were decided |
| `errors` | Attempts that did not complete |
| `errors_by_kind` | `errors` split by whose failure it was: `target`, `attacker`, `judge`, `infrastructure` |

### Success

`success` holds the success rates:

| Field | Meaning |
| --- | --- |
| `per_attempt_confirmed` | Confirmed hits over decided attempts |
| `per_attempt_suspected` | Suspected hits over decided attempts |
| `within_k` | Success within k target queries, one point per k (below) |
| `queries_to_first_confirmed_median`, `queries_to_first_confirmed_max` | For each probe with a confirmed hit, how many target queries it took to reach the first one, as their median and their largest value. `null` without a confirmed hit |

Success within k target queries treats each probe as one scenario. A probe's attempts run in order;
its first k target queries are those of the attempts up to the one that took the total past k. A
point of `within_k` has:

| Field | Meaning |
| --- | --- |
| `k` | The number of target queries |
| `probes` | Probes an attempt decided within k queries |
| `undecided` | The other probes: every attempt within k queries was undetermined, stopped or an error |
| `confirmed` | Probes with a confirmed hit within k queries, over `probes` |
| `suspected` | Probes with a suspected hit and no confirmed hit within k queries, over `probes` |

Up to 20 queries per probe, there is a point for every k. Beyond that, points are at 1, 2, 3, 5, 10,
20, 50, 100 and so on, and at the largest k. A run that did not record target queries, such as a
converted version 0.2 report, has no point.

### Refusals

`refusals` gives the attacker's refusals: `generations` (attacks it was asked to write), `refusals`
(those it refused or left empty), `rate` (refusals over generations) and `by_method`, one entry per
attack method with its `method`, `generations`, `refusals` and `rate`.

### Findings

`findings` has `confirmed`, `attempted` and `suspected` (findings counted as each),
`by_level` (for each level 1 to 5, its `level`, `label`, whether it is `counted_as` confirmed or
suspected, and how many `findings` reached it; level 3 holds attempted actions too) and `items`, one
per finding. A probe whose negative control showed the same signal has no finding.

| Field | Meaning |
| --- | --- |
| `id`, `probe_id` | The finding and its probe |
| `vulnerability`, `vulnerability_type`, `attack_method`, `objective_class` | What the probe tested |
| `evidence_level`, `label`, `counted_as` | The highest level its hits reached, its label, and `confirmed`, `attempted` or `suspected` |
| `severity`, `status` | The run file's severity, and the finding's status (`open`, `accepted`, `fixed`, `false_positive`) |
| `hits`, `attempts` | The probe's hits and attack attempts |
| `verification` | The verification outcome (`verified`, `consistent`, `not_reproducible`, `unverifiable`), or `not evaluated` |
| `reproductions` | `r of n`: repetitions that reproduced the hit at level 2 or above, of those that ran, or `not evaluated` |
| `negative_control` | `passed`, `failed`, `undetermined`, or `not evaluated` when no control ran for the probe |

### Verification and controls

`verification` counts the probes' verification outcomes: `verified`, `consistent`,
`not_reproducible` and `unverifiable`. `evaluated` is false when the run carries no verification,
and `note` then says so.

`controls` counts negative controls, the probe run without the attack: `ran`, `passed` (the target
held), `failed` (it showed the signal) and `undetermined`. They come from the run's control attempts,
or, when it has none, from its verifications. `evaluated` is false when no control ran, and `note`
then says so. The local engine fills both: its repetitions, negative controls and verifications are
described in the [run file guide](redteam-run-file.md#verification).

`judge_calibration` is the judge measured on the run file's labelled sample, or `null` without one:
the `judge` version, the `sample` size, `true_positive`, `false_positive`, `false_negative`,
`true_negative` and `undetermined` answers, `precision` (true positives over the hits it called) and
`recall` (true positives over the sample's hits) as rates, and `error` when it could not be measured.
It describes the judge; it never raises a judged hit above suspected.

### Groups and probes

`by_method` and `by_vulnerability` have one entry per attack method and per vulnerability, each with
its `name`, `probes`, `attempts`, `decided`, `undetermined`, `stopped` and `errors`, its
`confirmed` and `suspected` rates over decided attempts, the `target_queries` its attempts made and
`queries_to_first_hit_median`, the median of its probes' `queries_to_first_hit` (or `null`). A
method's entry also has its `kind` and its `arm` ([attack methods](redteam-methods.md#kinds-and-arms)):
`control` for the fixed templates, which send the same attacks every run, `static` for one written
attack, and `adaptive` for a method that reads the replies or the checks. Reading the control arm's
row beside each adaptive method's, over the same probes, is the static-versus-adaptive comparison;
the Markdown and HTML reports show the arm beside each method. A version 0.2 method is `static`, a
transform or a rewrite; a probe of a hosted campaign has neither.

`probes` has one entry per probe: its `id`, `vulnerability`, `vulnerability_type`,
`attack_method`, `objective_class`, the kinds of its `checks`, its attempts by outcome (`attempts`,
`hits`, `resisted`, `undetermined`, `not_delivered`, `stopped`, `errors`), the highest `evidence_level` a hit reached
(or `null`), the `target_queries` it made, `queries_to_first_hit`, the target queries up to its
first hit of any level (or `null`), and its benign task: `task_with_attack`, `k of n` attack attempts
in which it completed, and `task_without_attack`, `completed`, `not completed` or `undetermined` in
the negative control, each `not evaluated` when the probe has no benign task.

## Output formats

Every version 2 run writes `redteam-run.json`, `redteam-report.json` and `redteam-report.md`.
`martian redteam run FILE -f FORMAT` also writes the formats it names; `-f` can be repeated, takes
names separated by commas, and `all` names every one. An unknown name stops the run before anything
is called.

| Format | File | What it holds |
| --- | --- | --- |
| `html` | `redteam-report.html` | The report on one page: the gate, what attacked, success, outcomes, findings, verification and controls, refusals, groups, each attempt with its checks, and the limitations. Its styles are inline, it has no script and links to nothing, and its content security policy (`default-src 'none'`) stops a browser loading anything, so it opens with no network |
| `csv` | `redteam-attempts.csv` | One row per attempt: its probe, role, outcome, level, label, check results, error, spend, attack and reply. A cell that starts with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`, so a spreadsheet never runs it as a formula |
| `sarif` | `redteam.sarif` | SARIF 2.1.0: one rule per vulnerability and type, one result per finding, `error` when confirmed and `warning` when suspected, with the run file severity as GitHub's `security-severity`. Each result points at the run file, which GitHub code scanning needs, and keeps the same fingerprint from run to run |
| `junit` | `redteam-junit.xml` | One test suite per vulnerability and one test case per probe. A finding is a `failure` of type `confirmed` or `suspected`; a probe with an undetermined, stopped or errored attempt, or with no attempt, is an `error`, since none of these is a pass |

`martian redteam report RUN` writes the reports of a saved run again: `RUN` is a `redteam-run.json`
or the id of a run in the local store. It writes `redteam-report.json`, `redteam-report.md` and
every format, or those `-f` names, to `-o` (default: the folder of the `redteam-run.json`, else
`results`). `--run-file` gives the run file that SARIF results point at.

## Comparing two runs

`martian redteam compare BASELINE CANDIDATE` compares two runs, each a `redteam-run.json` or a run
id in the local store, on the probes both ran. It prints the comparison, and with `-o` also writes
`redteam-comparison.json` and `redteam-comparison.md` there. It exits 0 whatever it finds, and 2
when a run cannot be read. The comparison has:

| Field | Meaning |
| --- | --- |
| `baseline_run`, `candidate_run` | The two runs |
| `comparable` | False when anything that produced the numbers differs; `warnings` then says what |
| `warnings` | Each difference: the attackers or judges (model and settings), the services, the budgets (`limits`), the attempts per probe, the catalogue versions, the verdict rules, the attacker and judge prompts, or a probe's checks |
| `compared_probes` | Probes both runs ran. Probes only one ran are in `only_in_baseline` and `only_in_candidate`, and their findings are not compared |
| `new` | Findings of the candidate on a probe that had none in the baseline |
| `fixed` | Findings of the baseline on a probe that has none in the candidate, where the candidate decided at least one attack attempt of the probe |
| `unchanged` | Findings in both runs. `baseline_level` and `candidate_level` show when the level moved |
| `undecided` | Findings of the baseline on a probe the candidate decided no attempt of, so it cannot be called fixed |
| `paired` | The exact McNemar test on confirmed success of the attack attempts both runs decided, paired by probe and attempt number (`martian.evals.stats.compare_outcomes`): `compared` pairs, `improved` (a confirmed hit became held), `regressed`, the two rates, `effect`, `p_value`, `minimum_detectable_effect` and `label`: `better`, `worse` or `within_noise`. With fewer than six changed pairs it is always `within_noise` |
| `changed` | False when nothing differs: no new, fixed or undecided finding, no level moved and no pair changed. A run compared with itself shows no change |

Each entry of `new`, `fixed`, `unchanged` and `undecided` names the `probe_id`, `vulnerability`,
`vulnerability_type` and `attack_method`, the `status`, the two levels and labels
(`baseline_label`, `candidate_label`), and whether it is `counted_as` confirmed or suspected.

Runs that are not comparable are still compared, with the warning at the top. A change in success
between them may be the change of attacker or budget, not of the target.

## Pinned findings

A confirmed finding, or an attempted action, can be kept as a regression test, so that after a fix CI checks it stays fixed. A suspected finding cannot: it rests on a judge.

```bash
MARTIAN_REPORT_INCLUDE_CONTENT=1 martian redteam run redteam.yaml   # a run that keeps the attack text
martian redteam pin add results/redteam-run.json -o pins            # every finding but suspected ones, or --finding ID
martian redteam pin run redteam.yaml --pins pins                    # in CI: no attacker or judge model
```

`pin add` reads a `redteam-run.json`, or a run id in the local store, and writes one pin file per
finding to `-o` (default `pins`). A pin holds:

- the finding: its probe, vulnerability, type, attack method, objective, severity, level and label,
  and the run it came from;
- the exact attack that hit;
- its checks: the text, canary and tool-call checks that hit that attempt. This is the evidence
  predicate. A judge, receipt or state check needs a model, a listener or an environment, so it is
  not pinned, and a finding with no other evidence cannot be pinned. A suspected finding cannot be
  pinned;
- the recorded vulnerable reply: its text, tool calls and whether the target reported its tool
  calls.

A pin is written only when its checks still hit its recorded reply. A run that holds digests cannot
be pinned, since a pin needs the attack's text: run it with `MARTIAN_REPORT_INCLUDE_CONTENT=1`. A
pin holds the attack and the reply in clear text, so review it before committing it.

`pin run` builds the run file's target ([targets](redteam-targets.md)), behind the recording proxy when the run file lists `tools.proxy`, and for each pin first checks that its checks still hit its
recorded reply, then sends its attack once, in a session of its own, and applies its checks. It
calls no attacker or judge model and needs no model key or Martian connection. Each pin is:

| Status | Meaning |
| --- | --- |
| `still_reproduces` | A pinned check hit: the finding is back |
| `not_reproduced` | Every pinned check decided and none hit |
| `undetermined` | A pinned check could not decide, such as a tool-call check on a target that does not report its calls |
| `error` | The target failed or timed out |
| `invalid` | The pin's checks no longer hit its own recorded reply, so it proves nothing |

It writes `redteam-pins.json` (each pin's status, its check results and the counts by status, with
`passed`) and `redteam-pins.md` to `-o` or the run file's `output_folder`, and exits 0 when every pin
is `not_reproduced`, 1 otherwise, and 2 when it could not start.

## Pushing a run to a Martian server

`martian redteam push RUN` sends a finished version 2 run (a `redteam-run.json`, or the id of a run in the local store) to the Martian server `MARTIAN_API_URL` names, with `MARTIAN_API_TOKEN`: an API key with `redteam:run`, bound to no agent. The console then lists it with the runs the server ran. It goes to `PUT /v1/push/redteam/runs/{run_id}` ([red team API](redteam-api.md#pushed-runs)) as the run the local store keeps, so content the report leaves out stays out. The server labels it as executed by the client (`provenance.execution` is `client_executed`). Pushing it again changes nothing and says it is already there; a run with the same id and other content is refused. A run that has not finished is not pushed, and a refusal stops with exit code 2. A run in an environment is pushed this way when it ends ([environments](redteam-run-file.md#environments)); a failed push there is a warning.

## A planned campaign on a Martian server

`martian redteam campaign` starts the console's planned campaign on the Martian server `MARTIAN_API_URL` names, with `MARTIAN_API_TOKEN` (an API key with `redteam:run`, acting as the member who created it). The server's engine plans and runs it against a registered agent the organisation approved; nothing runs here. `--agent` names the agent (default: the one the key is bound to), `--depth` is `quick`, `standard` (the default) or `deep`, and `--objective`, `--outcome` and `--environment` narrow it as the console's wizard does. The console lists the campaign as soon as it starts, and the command prints where.

The command reads the campaign every `--poll` seconds (15) until it finishes, for at most `--timeout` seconds (7200); `--no-wait` only starts it. When it finished it writes the campaign as a run of the shared model to `redteam-run.json`, with `redteam-report.json` and `.md`, in `--output` (`results`). The exit code follows the backend's release decision:

| Release decision | Exit code |
| --- | --- |
| `APPROVED` | 0 |
| `CONDITIONAL` | 1, or 0 with a warning under `--allow-conditional` |
| `BLOCKED` | 1 |
| none: `PENDING`, a campaign that failed or did not finish in time, or a refused start | 2 |

## What a report holds of the target's content

Every format follows the same rule. Reports hold digests of the attacks, replies, judge reasons and
tool arguments, not the content, unless `MARTIAN_REPORT_INCLUDE_CONTENT=1`. Credentials are always
redacted. `martian redteam report` applies the rule again, so a saved run that holds content is
written with digests unless the variable is set. The report standard, SARIF and JUnit hold no
content at all: only counts, rates, ids and labels.
