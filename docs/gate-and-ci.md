# Gate and CI

The SDK can run standalone Python/YAML checks or one persisted session containing
evals, redteam, or both. Use one Backend origin/API token for both modules;
[SDK and CI](sdk-and-ci.md) defines auth, scope and the Python result contract.

```bash
martian redteam validate redteam.yaml  # No credentials or network requests.
martian redteam run redteam.yaml -o results -c 2 -a 1  # Local report/gate.
martian ci --redteam-config redteam.yaml --output results  # Persisted session.
martian ci --evals-path tests/evals --redteam-config redteam.yaml --require-export --output results
```

`martian redteam run` returns 0 for a passing local gate, 1 for a failed gate,
and 2 for setup/execution failures. It writes JSON/Markdown before applying the
gate. `martian ci` requires a complete Backend acknowledgment as well as complete
passing local results. Keep its typed receipt, report and probe checkpoints with
`if: always()`; a running/incomplete receipt never passes.

## Required behavior

- `score` is the integer 1 for resistance and 0 for vulnerability. Invalid/missing
  judgments, expired/unknown operation results and execution errors fail closed.
- Every planned required and informational probe must finish. A lower declared
  threshold affects required pass rates; it never excuses absent scores/errors.
- The persisted runner rejects ignored target/infrastructure failures, skipped or
  deselected coverage and empty required suites. Caught gate exceptions still fail.
- The pytest gate's `evaluate(...)` gives each `predict_fn` call a time limit,
  `target_timeout_seconds` (default 120); a call that outlasts it is that case's error and the
  gate fails closed on it. See [Evals](evals.md).
- Dataset outputs are canonically re-scored by Backend. Inline checks and redteam
  judgments remain declared client-executed/client-scored evidence, with catalog,
  graph/policy revisions and operation IDs; a CI upload is not binary attestation.
- Redteam execution POSTs run once. Ambiguous delivery uses GET of the same UUID;
  no execution retry can turn a failing judgment into a pass. Idempotent eval/session
  uploads have their own bounded transport retry policy.

## Customer workflows

[combined-ci.yml](../examples/combined-ci.yml) is the complete persisted example.
It installs a reviewed hash-checked wheel, configures `MARTIAN_API_URL`,
`MARTIAN_API_TOKEN`, agent/Git attribution and the customer's target credentials,
then runs both suites once and retains only safe reports. Omit either suite to
run the other. It does not request an Agents key or MLflow server credentials.
A downstream deployment should depend on that gate and deploy its tested artifact.

The root [composite action](../action.yml) and [reusable workflow](../.github/workflows/red-team.yml)
run **`martian redteam run`**, a local report/gate through Backend. They do not
persist a CI session. Both require a Backend URL and token with `redteam:run`.
The action accepts YAML path, output, `max-concurrent` and `attacks-per-vuln`.
Use `martian ci` for console session history/export requirements.

Pin SDK releases/commits and SHA-pin third-party actions. Use reviewed protected
refs/environments for credential access, contents-read checkout and
`persist-credentials: false`. Do not make the secret available to unreviewed PR
code. Validate configuration without secrets first. The callback has its own
provider/key setup; it is executed locally and can have side effects.

## Evals on a pull request, with one comment

The [evals action](../.github/actions/evals/action.yml) runs one
[suite file](evals.md#run-a-suite-file-locally) with `martian evals run` and
reports it on the pull request. Reference it as
`MorningHQ/martian-redteam/.github/actions/evals@<tag or full SHA>` after
`actions/checkout`. [examples/github-action](../examples/github-action/) holds
a [workflow](../examples/github-action/workflow.yml), a passing suite and a
failing one that is the passing suite with two replies broken, and the
comments they produce: [passed](../examples/github-action/comments/passed.md),
[failed](../examples/github-action/comments/failed.md), and
[compared](../examples/github-action/comments/compared.md), the failing suite
compared with a result of the passing one, as a pull request that broke two
of its cases would be compared with `main`: both newly fail a security check,
so it fails although two changed cases are within noise.

| Input | Default | Meaning |
| --- | --- | --- |
| `suite` | required | The suite file, relative to the workspace |
| `output` | `results/evals` | Folder for `results.jsonl`, `summary.json`, `junit.xml` and `verdicts.json`; one per suite |
| `github-token` | the workflow token | Writes the comment |
| `comment-key` | the suite path | Names the comment and the suite's baseline; give each job its own key when several jobs run one suite on a pull request, such as a model matrix |
| `comment-author` | `github-actions[bot]` | Login that writes the comment; change it only with another token |
| `baseline-branch` | empty: off | Branch whose last result a pull request is compared with, usually the default branch; see [compared with the last result of main](#compared-with-the-last-result-of-main) |

- **No Martian account.** The run is local, as on a laptop. A suite that grades
  stored replies with deterministic checks needs no secret at all. An LLM judge
  or a URL target reads its provider key or agent credential from the step's
  `env`.
- **One comment per suite.** The comment starts with a hidden marker,
  `<!-- martian-evals-comment: <key> -->`. Each run looks through the pull
  request's comments for that marker on a comment by `comment-author`, edits
  it, and creates one only when there is none. A comment by anyone else that
  quotes the marker is left alone. Give the job a `concurrency` group per pull
  request and suite, as the example does, so two quick pushes cannot both
  create it.
- **What it says.** The headline follows the exit code: passed (0), failed (1),
  or did not finish (2, a case without a verdict, a run that stopped at one of its
  [limits](evals.md#run-a-suite-file-locally) or an invalid suite file). A run
  that stopped says how many cases it did not run and which limit it reached;
  its counts, rates and reliability are of the cases it finished.
  A suite with a [policy check](evals.md#false-refusals) adds how many of its
  benign cases were refused, so a model that refuses everything cannot look
  as if it passed. Below it are per-check counts, each pass rate with its 95%
  interval ([pass rates and intervals](evals.md#pass-rates-and-confidence-intervals)),
  for a suite that [repeats its cases](evals.md#repeated-attempts) how many
  times each case ran, the rule each check's attempts combined by, a
  **Reliability** row under the pass rate and the
  [flaky cases](evals.md#reliability-and-flaky-cases),
  and up to 25 failed and 25 unfinished case
  IDs with the checks that did not pass. Inputs, replies and judge reasons stay
  out of the comment; a case without a verdict shows only the SDK's own
  target-failure wording. A run that stops before writing results says so
  instead of repeating an earlier result. The same text goes to the job summary
  on every event, and the comment is written only for `pull_request` events.
- **The exit code is the gate.** The action fails with the suite's exit code
  after reporting; compared with a baseline, that is the comparison's gate
  (below). It uploads nothing; upload `summary.json`, `junit.xml`,
  `verdicts.json` and `comparison.json` with `if: always()`, and keep
  `results.jsonl` and `mlflow.db` out of shared artifacts, because they hold
  prompts and replies.
- **Permissions.** The job needs `contents: read` and `pull-requests: write`,
  nothing more. Pull requests from forks and from Dependabot get a read-only
  token: GitHub refuses the comment with 403, the action prints a warning, and
  the result is in the job summary only. Any other GitHub API failure fails the
  step. Trigger on `pull_request`, never `pull_request_target`: the suite runs
  the pull request's code, and `pull_request_target` would give that code a
  write token and the repository's secrets.

### Compared with the last result of main

On its own, a run fails on any failed case, so a case that already fails on
`main` fails every pull request, and the comment cannot say what the pull
request changed. With `baseline-branch: main`, and the workflow triggered on
`push` to `main` as well as on `pull_request`, the action compares each pull
request with the last result of `main`, case by case:

- **Where the result is kept.** On a push to `baseline-branch`, a run in
  which every case has a verdict (exit 0 or 1) keeps its `verdicts.json` and
  the commit in the repository's GitHub Actions cache, one entry per
  `comment-key`. On a `pull_request`, the action restores the newest entry,
  from the pull request's base branch or the default branch, and runs
  `martian evals run <suite> --baseline <folder>`. The cache needs no
  permission in the job, no write access to the repository and no paid
  service, and only a push to `baseline-branch` writes the baseline: a cache
  entry written in a pull request's run is visible to that pull request only,
  and the action writes none. Other events, and a push to another branch, run
  without a baseline. To compare a run with a result kept some other way,
  run the command yourself with `--baseline`
  ([comparing with a baseline](evals.md#comparing-with-a-baseline)).
- **What is kept.** `verdicts.json` holds each case's id, verdict and check
  verdicts, a digest of its inputs and expectations, and the reasons of the
  deterministic checks that report counts, sizes or names. It holds no input,
  reply or judge reason, so a pull request from a fork, which can read the
  cache, sees no more than the case ids and verdicts.
- **The comparison** pairs a case of the pull request with the same case on
  `main`: the same id, inputs and expectations, and a verdict in both runs. The
  exact McNemar test labels the change **better**, **worse** or **within
  noise** ([comparing two runs](evals.md#comparing-two-runs)). With fewer than
  six changed cases a change is always within noise.
- **The gate.** Compared with `main`, the job fails when the change is worse
  than noise; when a case newly fails a security check, whatever the label; or
  when a case with no result on `main` to compare with failed: a new case, one
  whose inputs or expectations changed, or one without a verdict on `main`.
  Cases that failed on `main` too, and a few cases that went from pass to fail
  on other checks, within noise, do not fail it. A case without a verdict still
  fails the job.
- **Security checks fail the gate case by case.** One leaked secret or
  followed injection is never noise. A case that newly fails a
  [security check](evals.md#repeated-attempts) (`not_contains`, `not_matches`
  and the policy checks other than `no_refusal`, the types in
  `martian.evals.runner.SECURITY_CHECKS`) fails the job on its own: it fails
  the check here and passed it on `main`, or `main` had no such check, also
  when the case already failed another check on `main`. A case that failed the
  same security check on `main` is a known failure and does not fail the job.
  Other checks keep the noise rule, `no_refusal` and LLM judges among them,
  even a judge that guards a secret.
- **What the comment says.** The headline says `passed against main` or
  `failed against main`, then the label with the counts it rests on, the
  p-value and the smallest change it can tell, and the cases it could not pair.
  **Cases that newly failed a security check, which fails the gate** names each
  such case with those checks and their reasons; **Cases that went from pass to
  fail** names each other case that did, with its failed checks and their
  reasons.
  A reason that could quote the reply, an LLM judge's or a numeric, table or
  schema check's, shows as `—`; `results.jsonl` has it. A suite whose checks or
  attempts per case changed since `main` says so, because the label then
  measures that change too. `comparison.json` lists the security cases under
  `security_regressed`.
- **No baseline yet.** Until a push to `main` has kept a result for the suite,
  or after GitHub has evicted it (an entry unused for seven days, or beyond the
  repository's cache size), the comment says there is no baseline yet and the
  pull request is gated on its own, so any failed case fails it.

## Limits and privacy

Use small suites/concurrency first (`max_concurrent` defaults to 2). Admission
limits are enforced by Backend per key; shared network rate limits may also
reject work. A 429 is an execution error rather than a skipped gate.

Reports use input/output/reason digests by default. Content is opt-in and explicit
redaction flags take precedence; constructor credentials remain redacted after
client close. Keep private local MLflow SQLite/traces out of shared artifacts.
Backend scoring receives raw dataset outputs and judging receives attack/reply
text; approve those data flows before using production data.

Repository CI is separate: lint, strict types, Python 3.12/3.13 tests, package and
a credential-free action smoke (validate, and the evals action on both sample
suites, commenting on this repository's pull requests; the passing sample keeps
its result on `main` and compares each pull request with it). Fast tests use
fixtures and loopback services.
Real service/model/UI qualification is recorded in [validation](validation.md).
