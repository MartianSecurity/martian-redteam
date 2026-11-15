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
a [workflow](../examples/github-action/workflow.yml), a passing and a failing
suite, and the two comments they produce
([passed](../examples/github-action/comments/passed.md),
[failed](../examples/github-action/comments/failed.md)).

| Input | Default | Meaning |
| --- | --- | --- |
| `suite` | required | The suite file, relative to the workspace |
| `output` | `results/evals` | Folder for `results.jsonl`, `summary.json` and `junit.xml`; one per suite |
| `github-token` | the workflow token | Writes the comment |
| `comment-key` | the suite path | Names the comment; give each job its own key when several jobs run one suite on a pull request, such as a model matrix |
| `comment-author` | `github-actions[bot]` | Login that writes the comment; change it only with another token |

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
  or did not finish (2, a case without a verdict or an invalid suite file).
  A suite with a [policy check](evals.md#false-refusals) adds how many of its
  benign cases were refused, so a model that refuses everything cannot look
  as if it passed. Below it are per-check counts, each pass rate with its 95%
  interval ([pass rates and intervals](evals.md#pass-rates-and-confidence-intervals)),
  for a suite that [repeats its cases](evals.md#repeated-attempts) how many
  times each case ran, the rule each check's attempts combined by, a
  **Reliability** row under the pass rate and the
  [flaky cases](evals.md#reliability-and-flaky-cases), for a suite with
  [conversation cases](evals.md#conversations) a **Turns** row with the
  turns' own counts and how many cases are conversations,
  and up to 25 failed and 25 unfinished case
  IDs with the checks that did not pass and, for a conversation, the numbers of
  the turns that failed. Inputs, replies and judge reasons stay
  out of the comment; a case without a verdict shows only the SDK's own
  target-failure wording. A run that stops before writing results says so
  instead of repeating an earlier result. The same text goes to the job summary
  on every event, and the comment is written only for `pull_request` events.
- **The exit code is the gate.** The action fails with the suite's exit code
  after reporting. It uploads nothing; upload `summary.json` and `junit.xml`
  with `if: always()`, and keep `results.jsonl` and `mlflow.db` out of shared
  artifacts, because they hold prompts and replies.
- **Permissions.** The job needs `contents: read` and `pull-requests: write`,
  nothing more. Pull requests from forks and from Dependabot get a read-only
  token: GitHub refuses the comment with 403, the action prints a warning, and
  the result is in the job summary only. Any other GitHub API failure fails the
  step. Trigger on `pull_request`, never `pull_request_target`: the suite runs
  the pull request's code, and `pull_request_target` would give that code a
  write token and the repository's secrets.

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
suites, commenting on this repository's pull requests). Fast tests use fixtures
and loopback services.
Real service/model/UI qualification is recorded in [validation](validation.md).
