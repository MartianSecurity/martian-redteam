# Gate and CI

Use the checked-in [composite action](../action.yml), [reusable workflow](../.github/workflows/red-team.yml), or [example workflows](../examples/). They are executable references for input names and artifact handling.

- Validate configuration on pull requests without a Martian API key, in an isolated unprivileged job: validation imports the callback module and executes its top-level code.
- Run live assessments only in a trusted environment with MARTIAN_API_KEY and an explicit MARTIAN_API_URL.
- Start with concurrency 2 and one repeat; adjust to the service's measured quotas and budget.
- Pin the SDK/action to a reviewed release or commit, not a mutable branch.
- Preserve reports with an always-running artifact upload. Do not mask the live gate with continue-on-error.

```sh
uv run martian-redteam validate examples/config.yaml
uv run martian-redteam run examples/config.yaml -o results -c 2 -a 1
```

CLI run writes reports before applying the gate. Defaults fail on infrastructure errors and target errors; only gated vulnerability types participate in the pass-rate threshold. Read src/martian_redteam/gate.py and config.py for configurable behavior. Exit zero means the configured gate passed, not comprehensive agent security.

Python callers must call evaluate_gate themselves after receiving/saving the assessment. An empty configuration, unavailable graph or malformed response is not a successful test.

[Usage](usage.md) · [README](../README.md)
