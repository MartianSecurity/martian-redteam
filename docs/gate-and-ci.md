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
credential-free validate smoke. Fast tests use fixtures and loopback services.
Real service/model/UI qualification is recorded in [validation](validation.md).
